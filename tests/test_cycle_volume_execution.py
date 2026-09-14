import copy
import time
import unittest
from datetime import datetime, timezone
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trading.cycle import CyclePlan, DEFAULT_CYCLE, DailyVolumeLimitError
from trading.cycle_execution import CycleExecutor
from trading.exchange import ExchangeError, LiveBroker, RateBudget
from trading.models import TradingError, dec, wire
from trading.paper import PaperBroker
from trading.store import Store
from .helpers import Fixture


SYMBOL = "XAUUSD1"


class CycleVolumeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.account["cycle"] = {**DEFAULT_CYCLE, "enabled": True, "daily_volume_limit": "0"}
        self.f.store.save_account(self.f.account)
        self.progress = {"run_id": "volume-run", "phase": "waiting_open", "quantities": {"LONG": "0", "SHORT": "0"},
                         "opened_at": None, "completed_cycles": 0, "config": copy.deepcopy(self.f.account["cycle"])}
        self.f.store.put("cycle:test", self.progress)
        self.f.broker.set_cycle_leverage(SYMBOL, 2)
        self.executor = CycleExecutor(self.f.store, self.f.broker, self.f.market)

    def plan(self, phase="open"):
        book = self.f.market.book(SYMBOL)
        return CyclePlan(phase, SYMBOL, dec(2), 2, dec(2) * book.ask, dec(2) * book.bid, dec("0.02"))

    def open(self, callback=None):
        return self.executor.start(self.f.account, self.f.broker.cycle_snapshot([SYMBOL]), self.plan(),
                                   self.f.store.get("cycle:test"), before_submit=callback)

    def close_cycle(self):
        progress = self.f.store.get("cycle:test")
        progress.update(phase="waiting_close", opened_at=time.time() - 61)
        self.f.store.put("cycle:test", progress)
        return self.executor.start(self.f.account, self.f.broker.cycle_snapshot([SYMBOL]), self.plan("close"), progress)

    def limit(self, amount):
        self.f.account["cycle"]["daily_volume_limit"] = amount
        self.f.store.save_account(self.f.account)
        progress = self.f.store.get("cycle:test")
        progress["config"]["daily_volume_limit"] = amount
        self.f.store.put("cycle:test", progress)

    def test_unlimited_accounts_still_record_each_open_and_close_fill(self):
        self.open()
        opening = self.f.store.cycle_daily_volume("test")
        expected_open = Fraction(self.plan().long_notional) + Fraction(self.plan().short_notional)
        self.assertEqual(Fraction(dec(opening["volume"])), expected_open)
        self.assertEqual(opening["trade_count"], 2)
        self.close_cycle()
        complete = self.f.store.cycle_daily_volume("test")
        self.assertEqual(Fraction(dec(complete["volume"])), 2 * expected_open)
        self.assertEqual(complete["trade_count"], 4)
        self.assertEqual(self.f.store.cycle_volume_backlog("test"), [])

    def test_roundtrip_quota_blocks_before_any_order(self):
        self.limit("20000")
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not submit")), self.assertRaises(DailyVolumeLimitError):
            self.open()
        self.assertIsNone(self.f.store.intent("test"))

    def test_used_volume_blocks_next_cycle_and_quota_is_rechecked_after_callback(self):
        self.limit("40000")
        self.open()
        self.close_cycle()
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not submit")), self.assertRaises(DailyVolumeLimitError):
            self.open()
        self.limit("60000")
        daily = self.f.store.cycle_daily_volume("test")
        with patch.object(self.f.store, "cycle_daily_volume", side_effect=[{**daily, "volume": "0"}, {**daily, "volume": "50000"}]), \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("must not submit")), self.assertRaises(DailyVolumeLimitError):
            self.open()

    def test_accounting_read_failure_never_bypasses_quota(self):
        with patch.object(self.f.store, "cycle_daily_volume", side_effect=TradingError("ledger unavailable")), \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("must not submit")), self.assertRaisesRegex(TradingError, "ledger"):
            self.open()

    def test_missing_fill_details_block_next_open_but_allow_close(self):
        with patch.object(self.f.broker, "cycle_trades", side_effect=TradingError("fill details delayed")):
            self.open()
        self.assertEqual(self.f.store.get("cycle:test")["phase"], "holding")
        self.assertEqual(self.f.store.cycle_daily_volume("test")["volume"], "0")
        backlog = self.f.store.cycle_volume_backlog("test")
        self.assertEqual(len(backlog), 1)
        self.assertIn("delayed", backlog[0]["volume_error"])
        self.limit("1")
        self.close_cycle()
        self.assertEqual(tuple(p.qty for p in self.f.broker.cycle_snapshot([SYMBOL]).pair(SYMBOL)), (0, 0))
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not submit")), self.assertRaisesRegex(TradingError, "补齐"):
            self.open()
        self.assertTrue(self.executor.sync_volume(self.f.account, backlog[0]))
        self.assertEqual(self.f.store.cycle_daily_volume("test")["trade_count"], 4)

    def test_partial_open_repair_is_counted_even_if_limit_is_exceeded(self):
        self.limit("40000")
        original = self.f.broker.submit
        def one_leg(orders):
            if len(orders) == 2:
                result = original(orders[:1])[0]
                self.limit("1")
                return [result, {"code": -2019}]
            return original(orders)
        with patch.object(self.f.broker, "submit", side_effect=one_leg):
            self.open()
        records = self.f.store.cycle_trade_records("test")
        self.assertEqual({row["phase"] for row in records}, {"open", "repair"})
        self.assertEqual(len(records), 2)
        self.assertEqual(tuple(p.qty for p in self.f.broker.cycle_snapshot([SYMBOL]).pair(SYMBOL)), (0, 0))

    def test_fill_lookup_failure_never_prevents_partial_open_repair(self):
        original = self.f.broker.submit
        def one_leg(orders):
            return [original(orders[:1])[0], {"code": -2019}] if len(orders) == 2 else original(orders)
        with patch.object(self.f.broker, "submit", side_effect=one_leg) as submit, \
             patch.object(self.f.broker, "cycle_trades", side_effect=ExchangeError("rate wait", retry_after=60)):
            self.open()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(tuple(p.qty for p in self.f.broker.cycle_snapshot([SYMBOL]).pair(SYMBOL)), (0, 0))
        self.assertEqual(len(self.f.store.cycle_volume_backlog("test")), 1)

    def test_restart_repeated_sync_never_counts_a_fill_twice(self):
        with patch.object(self.executor, "sync_volume", return_value=False):
            self.open()
        restored = Store(self.f.store.path)
        broker = PaperBroker("test", self.f.market, restored)
        executor = CycleExecutor(restored, broker, self.f.market)
        backlog = restored.cycle_volume_backlog("test")[0]
        original = copy.deepcopy(backlog)
        self.assertTrue(executor.sync_volume(self.f.account, backlog))
        self.assertTrue(executor.sync_volume(self.f.account, original))
        self.assertEqual(restored.cycle_daily_volume("test")["trade_count"], 2)
        self.assertEqual(restored.cycle_volume_backlog("test"), [])

    def test_paper_fill_times_split_across_utc_midnight_after_restart(self):
        midnight = datetime(2026, 9, 14, tzinfo=timezone.utc).timestamp()
        original = self.f.broker.submit
        def cross_midnight(orders):
            result = []
            for order, stamp in zip(orders, (midnight - .001, midnight + .001)):
                with patch("trading.paper.time", SimpleNamespace(time=lambda: stamp)):
                    result.extend(original([order]))
            return result
        with patch.object(self.f.broker, "submit", side_effect=cross_midnight), patch.object(self.executor, "sync_volume", return_value=False):
            self.open()
        restored = Store(self.f.store.path)
        executor = CycleExecutor(restored, PaperBroker("test", self.f.market, restored), self.f.market)
        intent = restored.cycle_volume_backlog("test")[0]
        self.assertTrue(executor.sync_volume(self.f.account, intent))
        self.assertEqual(restored.cycle_daily_volume("test", now=midnight - 1)["trade_count"], 1)
        self.assertEqual(restored.cycle_daily_volume("test", now=midnight + 1)["trade_count"], 1)
        self.assertEqual({row["executed_at"] for row in restored.cycle_trade_records("test")}, {midnight - .001, midnight + .001})

    def test_legacy_paper_receipts_are_explicitly_estimated(self):
        with patch.object(self.executor, "sync_volume", return_value=False):
            self.open()
        intent = self.f.store.cycle_volume_backlog("test")[0]
        for receipt in intent["receipts"].values():
            for key in ("orderId", "paperTrades", "time", "updateTime"):
                receipt.pop(key, None)
        self.f.store.save_intent(intent)
        self.assertTrue(self.executor.sync_volume(self.f.account, intent))
        records = self.f.store.cycle_trade_records("test")
        self.assertEqual({row["time_source"] for row in records}, {"legacy_estimated"})
        self.assertEqual({row["executed_at"] for row in records}, {intent["order_times"][cid] for cid in intent["receipts"]})
        self.assertEqual(self.f.store.cycle_daily_volume("test")["estimated_trade_count"], 2)


class LiveCycleTradeTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.api = Mock()
        self.api.budget = None
        self.broker = LiveBroker({}, self.f.market, api=self.api)
        self.executor = CycleExecutor(self.f.store, self.broker, self.f.market)
        self.midnight = datetime(2026, 9, 14, tzinfo=timezone.utc).timestamp()
        self.order = self.executor.order(SYMBOL, "LONG", "BUY", dec(2), "volume-order")
        self.receipt = {"symbol": SYMBOL, "positionSide": "LONG", "side": "BUY", "clientOrderId": "volume-order",
                        "orderId": 101, "status": "FILLED", "executedQty": "2", "avgPrice": "100",
                        "time": int((self.midnight - 1) * 1000), "updateTime": int((self.midnight + 1) * 1000)}

    def trade(self, tid, qty="1", stamp=None, oid=101, **extra):
        return {"id": tid, "orderId": oid, "symbol": SYMBOL, "side": "BUY", "positionSide": "LONG", "qty": qty,
                "price": "100", "quoteQty": wire(dec(qty) * 100), "time": int((self.midnight if stamp is None else stamp) * 1000), **extra}

    def intent(self):
        intent = {"id": "live-volume", "kind": "cycle", "phase": "open", "account_id": "test", "symbol": SYMBOL,
                  "run_id": "live-run", "created_at": self.midnight - 2, "completed_at": time.time(), "status": "complete",
                  "orders": [self.order], "repairs": [], "receipts": {"volume-order": copy.deepcopy(self.receipt)}}
        self.f.store.save_intent(intent)
        return intent

    def test_live_fills_use_exchange_times_and_ignore_other_orders(self):
        self.api.call.return_value = [self.trade(1, stamp=self.midnight - .001), self.trade(2, stamp=self.midnight + .001), self.trade(3, qty="900", oid=999)]
        intent = self.intent()
        self.assertTrue(self.executor.sync_volume(self.f.account, intent))
        self.assertTrue(self.executor.sync_volume(self.f.account, intent))
        self.assertEqual(self.api.call.call_count, 1)
        self.assertEqual(self.f.store.cycle_daily_volume("test", now=self.midnight - 1)["volume"], "100")
        self.assertEqual(self.f.store.cycle_daily_volume("test", now=self.midnight + 1)["volume"], "100")

    def test_pagination_uses_from_id_without_time_or_order_id_parameters(self):
        first = [self.trade(i, oid=999) for i in range(1, 1000)] + [self.trade(1000)]
        self.api.call.side_effect = [first, [self.trade(1001)]]
        fills = self.broker.cycle_trades(self.order, self.receipt, self.midnight - 2)
        self.assertEqual([fill["trade_id"] for fill in fills], ["1000", "1001"])
        initial = self.api.call.call_args_list[0].args[2]
        following = self.api.call.call_args_list[1].args[2]
        self.assertIn("startTime", initial)
        self.assertIn("endTime", initial)
        self.assertEqual(following, {"symbol": SYMBOL, "limit": 1000, "fromId": 1001})
        self.assertNotIn("orderId", initial)

    def test_incomplete_details_are_partially_recorded_and_remain_backlogged(self):
        self.api.call.return_value = [self.trade(1)]
        intent = self.intent()
        self.assertFalse(self.executor.sync_volume(self.f.account, intent))
        self.assertEqual(self.f.store.cycle_daily_volume("test", now=self.midnight)["volume"], "100")
        self.assertEqual(len(self.f.store.cycle_volume_backlog("test")), 1)
        self.api.call.return_value = [self.trade(1), self.trade(2)]
        self.assertTrue(self.executor.sync_volume(self.f.account, intent))
        self.assertEqual(self.f.store.cycle_daily_volume("test", now=self.midnight)["volume"], "200")
        self.assertEqual(self.f.store.cycle_daily_volume("test", now=self.midnight)["trade_count"], 2)

    def test_missing_order_id_is_resolved_using_verified_client_order(self):
        intent = self.intent()
        intent["receipts"]["volume-order"].pop("orderId")
        self.api.call.return_value = [self.trade(1, qty="2")]
        with patch.object(self.broker, "query", return_value=self.receipt) as query:
            self.assertTrue(self.executor.sync_volume(self.f.account, intent))
        query.assert_called_once_with(SYMBOL, "volume-order")

    def test_empty_or_wrong_direction_details_are_never_reported_as_zero_complete(self):
        for rows in ([], [self.trade(1, qty="2", side="SELL")], [self.trade(1, qty="2", positionSide="SHORT")]):
            with self.subTest(rows=rows):
                self.api.call.return_value = rows
                with self.assertRaises(TradingError):
                    self.broker.cycle_trades(self.order, self.receipt, self.midnight - 2, checkpoint={})

    def test_bounded_pagination_checkpoint_resumes_after_broker_restart(self):
        pages = [[self.trade(i, oid=999) for i in range(1000 * n + 1, 1000 * (n + 1) + 1)] for n in range(5)]
        self.api.call.side_effect = pages
        checkpoint = {}
        with self.assertRaisesRegex(TradingError, "分页"):
            self.broker.cycle_trades(self.order, self.receipt, self.midnight - 2, checkpoint=checkpoint)
        self.assertEqual(checkpoint["from_id"], 5001)
        restored_api = Mock()
        restored_api.budget = None
        restored_api.call.return_value = [self.trade(5001, qty="2")]
        restored = LiveBroker({}, self.f.market, api=restored_api)
        fills = restored.cycle_trades(self.order, self.receipt, self.midnight - 2, checkpoint=copy.deepcopy(checkpoint))
        self.assertEqual(len(fills), 1)
        self.assertEqual(restored_api.call.call_args.args[2], {"symbol": SYMBOL, "limit": 1000, "fromId": 5001})

    def test_rate_limit_is_reported_as_unsynced_without_throwing(self):
        self.api.call.side_effect = ExchangeError("rate limited", code=-1003, retry_after=60)
        intent = self.intent()
        self.assertFalse(self.executor.sync_volume(self.f.account, intent))
        self.assertIn("rate limited", intent["volume_error"])
        self.assertEqual(len(self.f.store.cycle_volume_backlog("test")), 1)

    def test_accounting_never_consumes_order_repair_reserved_budget(self):
        budget = RateBudget()
        budget.weight = 1499
        self.api.budget = budget
        def request(method, path, params, **options):
            budget.require_available(options["weight"])
            return [self.trade(1, qty="2")]
        self.api.call.side_effect = request
        intent = self.intent()
        with self.broker.reconciliation_budget():
            self.assertFalse(self.executor.sync_volume(self.f.account, intent))
            # The outer repair context is restored, with its reserve intact.
            budget.require_available(50)
        self.assertEqual(self.f.store.cycle_daily_volume("test", now=self.midnight)["trade_count"], 0)

    def many_fills(self, count=20001):
        quantity = wire(Fraction(count, 1000))
        self.order["quantity"] = quantity
        self.receipt["executedQty"] = quantity
        return [{"trade_id": str(index), "order_id": "101", "client_id": "volume-order", "symbol": SYMBOL,
                 "position_side": "LONG", "side": "BUY", "quantity": "0.001", "price": "100",
                 "notional": "0.1", "executed_at": self.midnight + index / 1000, "time_source": "exchange"}
                for index in range(1, count + 1)]

    def test_order_with_over_twenty_thousand_fills_is_recorded_in_bounded_chunks(self):
        fills = self.many_fills()
        intent = self.intent()
        with patch.object(self.broker, "cycle_trades", return_value=fills), \
             patch.object(self.f.store, "record_cycle_fills", wraps=self.f.store.record_cycle_fills) as record:
            self.assertTrue(self.executor.sync_volume(self.f.account, intent))
        self.assertEqual(record.call_count, 21)
        self.assertTrue(all(len(call.args[1]) <= 1000 for call in record.call_args_list))
        daily = self.f.store.cycle_daily_volume("test", now=self.midnight)
        self.assertEqual(daily["trade_count"], 20001)
        self.assertEqual(dec(daily["volume"]), dec("2000.1"))
        self.assertEqual(self.f.store.cycle_volume_backlog("test"), [])

    def test_large_partial_fill_chunk_failure_restarts_without_duplicate_accounting(self):
        fills = self.many_fills()
        intent = self.intent()
        original_record = self.f.store.record_cycle_fills
        writes = []
        def interrupted_record(saved, rows):
            writes.append(len(rows))
            if len(writes) == 2:
                raise TradingError("simulated chunk interruption")
            return original_record(saved, rows)
        def partial_lookup(order, receipt, created_at, *, checkpoint):
            checkpoint["fills"] = {fill["trade_id"]: fill for fill in fills}
            raise TradingError("remaining fill details delayed")
        with patch.object(self.broker, "cycle_trades", side_effect=partial_lookup), \
             patch.object(self.f.store, "record_cycle_fills", side_effect=interrupted_record):
            self.assertFalse(self.executor.sync_volume(self.f.account, intent))
        self.assertEqual(writes, [1000, 1000])
        self.assertEqual(self.f.store.cycle_daily_volume("test", now=self.midnight)["trade_count"], 1000)
        self.assertEqual(len(self.f.store.cycle_volume_backlog("test")), 1)
        restored = Store(self.f.store.path)
        restored_executor = CycleExecutor(restored, self.broker, self.f.market)
        resumed = restored.cycle_volume_backlog("test")[0]
        def complete_lookup(order, receipt, created_at, *, checkpoint):
            checkpoint.clear()
            return fills
        with patch.object(self.broker, "cycle_trades", side_effect=complete_lookup):
            self.assertTrue(restored_executor.sync_volume(self.f.account, resumed))
            self.assertTrue(restored_executor.sync_volume(self.f.account, resumed))
        daily = restored.cycle_daily_volume("test", now=self.midnight)
        self.assertEqual(daily["trade_count"], 20001)
        self.assertEqual(dec(daily["volume"]), dec("2000.1"))
        self.assertEqual(restored.cycle_volume_backlog("test"), [])


if __name__ == "__main__":
    unittest.main()
