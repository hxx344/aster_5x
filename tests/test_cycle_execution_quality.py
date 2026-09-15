import copy
from decimal import localcontext
from fractions import Fraction
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trading.cycle import CyclePlan, DEFAULT_CYCLE
from trading.cycle_execution import CycleExecutor
from trading.cycle_quality import estimate, number
from trading.depth import DepthSnapshot
from trading.exchange import AmbiguousOrder, ExchangeError, RequestNotSent
from trading.models import TradingError, dec
from trading.paper import PaperBroker
from trading.store import Store
from tests.helpers import Fixture, account


class CycleExecutionQualityTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.account["cycle"] = {**DEFAULT_CYCLE, "enabled": True}
        self.f.store.save_account(self.f.account)
        self.progress = {"run_id": "quality-test", "phase": "waiting_open", "quantities": {"LONG": "0", "SHORT": "0"},
                         "opened_at": None, "completed_cycles": 0, "config": copy.deepcopy(self.f.account["cycle"])}
        self.f.store.put("cycle:test", self.progress)
        self.f.broker.set_cycle_leverage("XAUUSD1", 2)
        self.executor = CycleExecutor(self.f.store, self.f.broker, self.f.market)

    def start(self, phase="open", *, trigger=None, before_submit=None):
        book = self.f.market.book("XAUUSD1")
        plan = CyclePlan(phase, "XAUUSD1", dec(2), 2, dec(2) * book.ask, dec(2) * book.bid, dec("0.02"))
        progress = self.f.store.get("cycle:test")
        if phase == "close":
            progress.update(opened_at=time.time() - 61, phase="waiting_close")
            self.f.store.put("cycle:test", progress)
        return self.executor.start(self.f.account, self.f.broker.cycle_snapshot(["XAUUSD1"]), plan, progress,
                                   before_submit, trigger=trigger)

    def quality(self):
        return self.f.store.get("cycle_execution:test")

    def test_paper_full_pair_uses_batch_quantity_and_actual_submit_clock(self):
        now = time.time()
        trigger_depth = DepthSnapshot(((Fraction(4412), Fraction(1)), (Fraction(4411), Fraction(3))),
                                      ((Fraction(4413), Fraction(1)), (Fraction(4414), Fraction(3))), now)
        final_depth = self.f.market.depth("XAUUSD1")
        trigger = {"source": "bbo", "received_at": now - .1, "received_monotonic": 99.9,
                   "checked_at": now, "depth": trigger_depth}
        final = {"depth": final_depth, "checked_at": now, "checked_monotonic": 99.995}
        with patch("trading.cycle_execution.clock_tick", return_value=None), \
             patch("trading.cycle_quality.clock_tick", return_value=None), \
             patch("trading.cycle_quality.time.monotonic", side_effect=[100, 100.025]), \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.start(trigger=trigger, before_submit=lambda snapshot: final)
        self.assertEqual(submit.call_count, 1)
        quality = self.quality()
        self.assertEqual((quality["phase"], quality["quantity"]), ("open", "2"))
        self.assertEqual(quality["trigger_estimate"]["buy_vwap"], "4413.5")
        self.assertEqual(quality["trigger_estimate"]["sell_vwap"], "4411.5")
        self.assertEqual(quality["final_estimate"]["sampled_at"], final_depth.timestamp)
        self.assertEqual(quality["final_estimate"]["buy_vwap"], "4412.02")
        self.assertEqual(quality["actual"]["buy_vwap"], "4412.02")
        self.assertEqual(quality["actual"]["sell_vwap"], "4412.01")
        self.assertEqual(quality["actual"]["spread_bp"], number(Fraction(1, 100) * 20000 / Fraction("8824.03")))
        self.assertEqual(quality["actual"]["status"], "filled")
        self.assertTrue(quality["actual"]["confirmed"])
        self.assertAlmostEqual(quality["timing"]["trigger_to_request_ms"], 100)
        self.assertAlmostEqual(quality["timing"]["final_check_to_request_ms"], 5)
        self.assertAlmostEqual(quality["timing"]["request_to_response_ms"], 25)
        self.assertEqual(quality["timing"]["request_status"], "returned")
        self.assertEqual(self.executor.last_completed_intent["execution_quality"], quality)
        encoded = json.dumps(quality, allow_nan=False)
        self.assertNotIn("monotonic", encoded)
        self.assertNotIn("DepthSnapshot", encoded)
        with self.f.store.connect() as db:
            saved = json.loads(db.execute("SELECT data FROM intents WHERE id=?", (quality["intent_id"],)).fetchone()[0])
        self.assertEqual(saved["execution_quality"], quality)

    def test_close_buy_is_short_and_sell_is_long(self):
        self.start()
        opening = self.quality()
        self.start("close")
        quality = self.quality()
        self.assertEqual(quality["phase"], "close")
        self.assertNotEqual(quality["intent_id"], opening["intent_id"])
        self.assertEqual(quality["actual"]["status"], "filled")
        self.assertEqual(quality["actual"]["buy_vwap"], "4412.02")
        self.assertEqual(quality["actual"]["sell_vwap"], "4412.01")
        closed = self.executor.last_completed_intent
        buy = next(order for order in closed["orders"] if order["side"] == "BUY")
        self.assertEqual(buy["positionSide"], "SHORT")
        self.assertEqual(self.f.store.get("cycle:test")["completed_cycles"], 1)

    def test_partial_pair_and_repair_do_not_become_a_complete_spread(self):
        original = self.f.broker.submit
        def partial(orders):
            return [original(orders[:1])[0], {"code": -2019}] if len(orders) == 2 else original(orders)
        with patch.object(self.f.broker, "submit", side_effect=partial) as submit:
            self.start()
        quality = self.quality()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(quality["actual"]["status"], "partial")
        self.assertEqual(quality["actual"]["buy_quantity"], "2")
        self.assertEqual(quality["actual"]["sell_quantity"], "0")
        self.assertTrue(quality["actual"]["repairs_present"])
        self.assertFalse(quality["actual"]["confirmed"])
        for key in ("buy_vwap", "sell_vwap", "spread_bp", "quantity"):
            self.assertIsNone(quality["actual"][key])
        self.assertEqual(self.f.store.get("cycle:test")["completed_cycles"], 0)

    def test_partial_close_repair_does_not_supply_missing_original_leg(self):
        self.start()
        original = self.f.broker.submit
        def partial(orders):
            return [original(orders[:1])[0], {"code": -2019}] if len(orders) == 2 else original(orders)
        with patch.object(self.f.broker, "submit", side_effect=partial):
            self.start("close")
        quality = self.quality()
        self.assertEqual(quality["actual"]["status"], "partial")
        self.assertEqual(quality["actual"]["sell_quantity"], "2")
        self.assertEqual(quality["actual"]["buy_quantity"], "0")
        self.assertIsNone(quality["actual"]["spread_bp"])
        self.assertEqual(self.f.store.get("cycle:test")["completed_cycles"], 1)

    def test_timeout_can_later_confirm_fills_without_faking_a_response(self):
        original = self.f.broker.submit
        def accepted(orders):
            original(orders)
            raise AmbiguousOrder("accepted then timed out")
        with patch.object(self.f.broker, "submit", side_effect=accepted) as submit:
            self.start()
        quality = self.quality()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(quality["actual"]["status"], "filled")
        self.assertEqual(quality["timing"]["request_status"], "failed")
        self.assertIsNotNone(quality["timing"]["request_started_at"])
        self.assertIsNone(quality["timing"]["response_received_at"])
        self.assertIsNone(quality["timing"]["request_to_response_ms"])

    def test_unknown_receipts_remain_unknown_without_another_submission(self):
        with patch.object(self.f.broker, "submit", side_effect=AmbiguousOrder("unknown write")) as submit, \
             patch.object(self.f.broker, "query", side_effect=ExchangeError("not visible", code=-2013)):
            self.start()
            self.executor.reconcile(self.f.account)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.quality()["actual"]["status"], "unknown")
        self.assertIsNone(self.quality()["actual"]["spread_bp"])

    def test_local_rejection_is_known_not_sent_and_has_no_response_time(self):
        with patch.object(self.f.broker, "submit", side_effect=RequestNotSent("local budget")) as submit:
            self.start()
        quality = self.quality()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(quality["actual"]["status"], "not_sent")
        self.assertEqual(quality["timing"]["request_status"], "not_sent")
        self.assertIsNone(quality["timing"]["request_to_response_ms"])

    def test_exchange_rejection_has_a_response_but_no_full_spread(self):
        with patch.object(self.f.broker, "submit", return_value=[{"code": -2019}, {"code": -2019}]) as submit:
            self.start()
        quality = self.quality()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(quality["actual"]["status"], "rejected")
        self.assertEqual(quality["timing"]["request_status"], "returned")
        self.assertIsNotNone(quality["timing"]["request_to_response_ms"])
        self.assertIsNone(quality["actual"]["spread_bp"])

    def test_nonterminal_zero_fills_are_pending(self):
        rows = {}
        def accepted(orders):
            for order in orders:
                cid = order["newClientOrderId"]
                rows[cid] = {**order, "clientOrderId": cid, "status": "NEW", "executedQty": "0", "avgPrice": "0"}
            return list(rows.values())
        with patch.object(self.f.broker, "submit", side_effect=accepted) as submit, \
             patch.object(self.f.broker, "query", side_effect=lambda symbol, cid: rows[cid]):
            self.start()
        quality = self.quality()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(quality["actual"]["status"], "pending")
        self.assertFalse(quality["actual"]["confirmed"])
        self.assertIsNone(quality["actual"]["spread_bp"])

    def test_restart_preserves_known_times_and_leaves_missing_clocks_unknown(self):
        with patch.object(self.executor, "reconcile", return_value="simulated crash"):
            self.start()
        before = self.quality()
        restored_store = Store(self.f.store.path)
        broker = PaperBroker("test", self.f.market, restored_store)
        executor = CycleExecutor(restored_store, broker, self.f.market)
        with patch.object(broker, "submit", side_effect=AssertionError("never resend")):
            executor.reconcile(self.f.account)
        self.assertEqual(self.quality()["timing"], before["timing"])
        self.assertIsNone(self.quality()["timing"]["trigger_to_request_ms"])
        self.assertIsNone(self.quality()["timing"]["final_check_to_request_ms"])
        self.assertEqual(self.quality()["actual"]["status"], "filled")
        historical = copy.deepcopy(executor.last_completed_intent)
        historical.pop("execution_quality")
        self.f.store.save_intent(historical)
        with patch.object(broker, "submit", side_effect=AssertionError("never resend")):
            executor.reconcile(self.f.account, historical)
        self.assertEqual(self.quality()["timing"]["request_status"], "unknown")
        self.assertTrue(all(value is None for key, value in self.quality()["timing"].items()
                            if key not in ("request_status", "pre_submit", "database")))
        self.assertTrue(all(value is None for value in self.quality()["timing"]["pre_submit"].values()))

    def test_final_estimate_reads_only_local_cache_and_no_extra_receipt_queries(self):
        depth = self.f.market.depth("XAUUSD1")
        stream = SimpleNamespace(snapshot=Mock(return_value=depth))
        self.f.market.depth_stream = stream
        with patch.object(self.f.market, "depth", side_effect=AssertionError("no REST fallback")), \
             patch.object(self.f.broker, "query", wraps=self.f.broker.query) as query, \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.start()
        self.assertEqual(stream.snapshot.call_count, 1)
        self.assertEqual(query.call_count, 0)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.quality()["final_estimate"]["status"], "available")

    def test_observation_and_display_write_failures_do_not_block_execution(self):
        self.f.market.depth_stream = SimpleNamespace(snapshot=Mock(side_effect=RuntimeError("bad cache")))
        with patch.object(self.f.store, "record_cycle_execution_quality", side_effect=RuntimeError("display unavailable")), \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.start(trigger={"depth": object(), "received_monotonic": float("nan")})
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.f.store.get("cycle:test")["phase"], "holding")
        self.assertIsNone(self.f.store.intent("test"))

    def test_trading_precheck_error_propagates_and_never_sends(self):
        def rejected(snapshot):
            raise TradingError("actual trading gate")
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not send")), \
             self.assertRaisesRegex(TradingError, "actual trading gate"):
            self.start(before_submit=rejected)
        self.assertIsNone(self.quality())

    def test_store_isolates_accounts_and_old_batches_cannot_replace_latest(self):
        self.start()
        opened = copy.deepcopy(self.executor.last_completed_intent)
        self.start("close")
        closed = self.quality()
        self.executor.reconcile(self.f.account, opened)
        self.assertEqual(self.quality()["intent_id"], closed["intent_id"])
        self.f.store.save_account(account("other"))
        tampered = copy.deepcopy(opened)
        tampered["account_id"] = "other"
        with self.assertRaisesRegex(TradingError, "账户"):
            self.f.store.record_cycle_execution_quality(tampered)
        self.assertIsNone(self.f.store.get("cycle_execution:other"))
        self.assertEqual(self.quality()["intent_id"], closed["intent_id"])

    def test_quality_only_write_preserves_terminal_status_and_volume_checkpoint(self):
        self.start()
        intent = self.executor.last_completed_intent
        with self.f.store.connect() as db:
            before = dict(db.execute("SELECT * FROM cycle_volume_sync WHERE intent_id=?", (intent["id"],)).fetchone())
        stale = copy.deepcopy(intent)
        stale["status"] = "pending"
        self.f.store.record_cycle_execution_quality(stale)
        with self.f.store.connect() as db:
            after = dict(db.execute("SELECT * FROM cycle_volume_sync WHERE intent_id=?", (intent["id"],)).fetchone())
            status = db.execute("SELECT status FROM intents WHERE id=?", (intent["id"],)).fetchone()[0]
        self.assertEqual(before, after)
        self.assertEqual(status, "complete")
        self.assertIsNotNone(after["synced_at"])


class QualityEstimateTests(unittest.TestCase):
    def test_missing_depth_does_not_extrapolate_or_relabel_stale_quotes(self):
        now = time.time()
        depth = DepthSnapshot(((Fraction(99), Fraction(1)),), ((Fraction(101), Fraction(2)),), now)
        self.assertEqual(estimate("2", depth, now)["status"], "unavailable")
        stale = DepthSnapshot(depth.bids, depth.asks, now - 4)
        self.assertEqual(estimate("1", stale, now)["status"], "unavailable")
        invalid = DepthSnapshot(depth.bids, depth.asks, now, validity=lambda: False)
        self.assertEqual(estimate("1", invalid, now)["status"], "unavailable")

    def test_display_precision_is_independent_of_global_decimal_context(self):
        with localcontext() as context:
            context.prec = 2
            self.assertEqual(number(Fraction(2, 3)), "0." + "6" * 35 + "7")
            self.assertEqual(number(Fraction("4412.01")), "4412.01")


if __name__ == "__main__":
    unittest.main()
