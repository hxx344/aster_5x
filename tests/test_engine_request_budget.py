from dataclasses import replace
import threading
import time
import unittest
from unittest.mock import Mock, patch

from trading.engine import Engine, PUBLIC_POLL_ALLOWANCE
from trading.exchange import API, MarketData, RateBudget
from trading.models import dec
from trading.store import Store
from .helpers import Fixture, account


class EngineRequestBudgetTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.engine.poll_market("XAUUSD1")
        self.engine.markets["XAUUSD1"]["capacities"] = {"4": "500000"}

    def expire(self, orders):
        return [{**o, "clientOrderId": o["newClientOrderId"], "status": "EXPIRED", "executedQty": "0", "avgPrice": "0"}
                for o in orders]

    def test_full_batch_uses_two_snapshots_and_clears_post_fill_check(self):
        with patch.object(self.f.broker, "snapshot", wraps=self.f.broker.snapshot) as snapshots:
            self.engine.tick_account("test")
        self.assertEqual(snapshots.call_count, 2)
        self.assertIsNone(self.f.store.intent("test"))
        self.assertIsNone(self.f.store.get("post_fill_check:test"))
        self.assertGreater(self.f.broker.snapshot(["XAUUSD1"]).pair("XAUUSD1")[0].qty, 0)
        self.assertEqual(self.engine.state()["accounts"][0]["status"], "running")

    def test_stale_completed_snapshot_is_read_again_before_risk_check(self):
        current = self.f.broker.snapshot(["XAUUSD1"])
        executor = Mock(last_snapshot=replace(current, timestamp=time.time() - 9))
        with patch.object(self.f.broker, "snapshot", return_value=current) as read:
            result = self.engine.completed_snapshot(executor, self.f.broker, ["XAUUSD1"])
        self.assertIs(result, current)
        read.assert_called_once_with(["XAUUSD1"])

    def test_low_actual_leverage_waits_even_with_existing_holdings_and_current_capacity(self):
        for leverage in (1, 2, 3):
            with self.subTest(leverage=leverage):
                self.f.broker.state["leverages"]["XAUUSD1"] = leverage
                for side in ("LONG", "SHORT"):
                    self.f.broker.state["positions"]["XAUUSD1:" + side] = {"qty": "0.01", "entry": "4412"}
                self.engine.markets["XAUUSD1"]["capacities"] = {str(leverage): "500000", "4": "0", "5": "0"}
                with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not open below 4x")), \
                     patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("no higher capacity")):
                    self.engine.tick_account("test")
                self.assertIsNone(self.f.store.intent("test"))
                self.assertIn("低于 4x", self.engine.state()["accounts"][0]["strategies"]["XAUUSD1"]["reason"])

    def test_zero_fill_cooldown_survives_restart_and_does_not_resubmit(self):
        with patch.object(self.f.broker, "submit", side_effect=self.expire) as send, \
             patch.object(self.f.broker, "snapshot", wraps=self.f.broker.snapshot) as snapshots:
            self.engine.tick_account("test")
        self.assertEqual(send.call_count, 1)
        self.assertEqual(snapshots.call_count, 2)
        stored = self.f.store.get("order_cooldown:test:XAUUSD1")
        self.assertEqual(stored["failures"], 1)
        self.assertGreater(stored["until"], time.time())
        restored = Engine(Store(self.f.store.path), market=self.f.market)
        restored.brokers["test"] = self.f.broker
        restored.poll_market("XAUUSD1")
        restored.markets["XAUUSD1"]["capacities"] = {"4": "500000"}
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("cooldown must persist")):
            restored.tick_account("test")
        self.assertIn("冷却中", restored.state()["accounts"][0]["reason"])

    def test_repeated_rejections_back_off_and_success_clears_cooldown(self):
        key = "order_cooldown:test:XAUUSD1"
        for expected in (30, 60, 120, 120):
            previous = self.f.store.get(key)
            if previous:
                self.f.store.put(key, {**previous, "until": time.time() - 1})
            started = time.time()
            with patch.object(self.f.broker, "submit", return_value=[{"code": -2019}, {"code": -2027}]):
                self.engine.tick_account("test")
            self.assertAlmostEqual(self.f.store.get(key)["until"] - started, expected, delta=1)
        previous = self.f.store.get(key)
        self.f.store.put(key, {**previous, "until": time.time() - 1})
        self.engine.tick_account("test")
        self.assertIsNone(self.f.store.get(key))

    def test_cooldown_does_not_block_authorized_upgrade(self):
        self.f.store.put("order_cooldown:test:XAUUSD1", {"failures": 2, "until": time.time() + 120})
        self.f.broker.state["leverages"]["XAUUSD1"] = 1
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("confirm leverage first")):
            self.engine.tick_account("test")
        self.assertEqual(self.f.store.intent("test")["target"], 4)

    def test_cooldown_does_not_block_pending_reconciliation(self):
        self.f.store.put("order_cooldown:test:XAUUSD1", {"failures": 2, "until": time.time() + 120})
        intent = {"id": "pending", "kind": "leverage", "symbol": "XAUUSD1", "account_id": "test",
                  "status": "pending", "previous": 1, "target": 4, "created_at": time.time()}
        self.f.store.save_intent(intent)
        self.engine.tick_account("test")
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.f.store.get("open_after_leverage:test:XAUUSD1"), 4)

    def test_many_account_intervals_fit_ordinary_budget_with_public_allowance(self):
        for active in range(9):
            accounts = [{**account("a" + str(i), mode="live"), "enabled": i < active} for i in range(8)]
            schedule = self.engine.scheduling(accounts)
            planned = PUBLIC_POLL_ALLOWANCE
            for row in accounts:
                timing = schedule[row["id"]]
                cost = 300 if row["enabled"] else 90
                self.assertGreaterEqual(timing["interval"], 10 if row["enabled"] else 60)
                planned += cost * 60 / timing["interval"]
            self.assertLessEqual(planned, 1500.000001)

    def test_warm_flat_or_top_tier_account_uses_faster_schedule_than_upgrade(self):
        row = account(mode="live")
        unknown = self.engine.scheduling([row])["test"]
        self.engine.view("test", snapshot={"positions": [
            {"symbol": "XAUUSD1", "leverage": 4, "qty": "0", "side": side} for side in ("LONG", "SHORT")]})
        warm = self.engine.scheduling([row])["test"]
        self.assertLess(warm["gap"], unknown["gap"])
        self.engine.markets["XAUUSD1"]["capacities"]["5"] = "500000"
        self.engine.view("test", snapshot={"positions": [
            {"symbol": "XAUUSD1", "leverage": 4, "qty": "1", "side": side} for side in ("LONG", "SHORT")]})
        upgrading = self.engine.scheduling([row])["test"]
        self.assertEqual(upgrading["gap"], unknown["gap"])

    def test_lower_official_budget_slows_ordinary_schedule(self):
        budget = RateBudget()
        budget.limit = 900
        api = API(budget=budget)
        self.addCleanup(api.close)
        self.engine.market = MarketData(api)
        rows = [account("live", mode="live")]
        timing = self.engine.scheduling(rows)["live"]
        self.assertGreater(timing["gap"], 10)
        self.assertGreater(timing["interval"], 10)

    def test_start_wakes_account_without_waiting_for_paused_interval(self):
        self.engine.enable("test", False)
        first, second = threading.Event(), threading.Event()
        calls = []
        def tick(aid):
            calls.append(aid)
            (first if len(calls) == 1 else second).set()
            return 60
        with patch.object(self.engine, "tick_account", side_effect=tick), \
             patch.object(self.engine, "poll_market", return_value=60), \
             patch.object(self.engine, "notify", return_value=60):
            self.engine.start()
            try:
                self.assertTrue(first.wait(1))
                self.engine.enable("test", True)
                self.assertTrue(second.wait(1), "start was delayed by the old paused due time")
            finally:
                self.engine.stop()

    def test_ordinary_live_accounts_are_staggered_and_pending_bypasses_gap(self):
        rows = [{**account("live" + str(i), mode="live"), "enabled": False} for i in range(3)]
        for row in rows:
            self.f.store.save_account(row)
        self.f.store.save_intent({"id": "recover", "kind": "pair", "account_id": "live2", "symbol": "XAUUSD1", "status": "pending"})
        seen, lock = {}, threading.Lock()
        done = threading.Event()
        def tick(aid):
            with lock:
                seen.setdefault(aid, time.monotonic())
                if all(a["id"] in seen for a in rows):
                    done.set()
            return 60
        timings = {row["id"]: {"interval": 60, "gap": .25} for row in rows}
        with patch.object(self.engine, "scheduling", return_value=timings), \
             patch.object(self.engine, "tick_account", side_effect=tick), \
             patch.object(self.engine, "poll_market", return_value=60), \
             patch.object(self.engine, "notify", return_value=60):
            self.engine.start()
            try:
                self.assertTrue(done.wait(2))
            finally:
                self.engine.stop()
        self.assertGreaterEqual(seen["live1"] - seen["live0"], .20)
        self.assertLess(seen["live2"], seen["live1"])


if __name__ == "__main__":
    unittest.main()
