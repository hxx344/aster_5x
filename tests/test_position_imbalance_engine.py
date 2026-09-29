"""Risk observations use existing evidence and never enter exchange code."""
from contextlib import ExitStack
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.helpers import Fixture, account
from trading import monitoring, position_observations as observations
from trading.engine import Engine
from trading.exchange import LiveBroker
from trading.models import TradingError
from trading.store import Store, dumps


def snapshot(now, long="2", short="1", symbol="XAUUSD1", orders=None):
    return {"timestamp": now, "open_orders": orders,
            "positions": [{"symbol": symbol, "side": side, "qty": qty}
                          for side, qty in (("LONG", long), ("SHORT", short))]}


def pending(status="pending", kind="pair"):
    order = {"symbol": "XAUUSD1", "positionSide": "LONG", "side": "BUY", "quantity": "1", "newClientOrderId": "leg"}
    return {"id": "intent", "account_id": "test", "status": status, "kind": kind, "symbol": "XAUUSD1",
            "orders": [order], "repairs": [], "receipts": {"leg": {**order, "clientOrderId": "leg", "status": "FILLED",
                "executedQty": "1", "avgPrice": "3000"}}}


class PositionObservationTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.account = {**self.f.account, "mode": "live", "enabled": False}
        self.f.store.save_account(self.account)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.addCleanup(self.engine.dashboard_reports.close)
        self.now = 1000.0
        clock = patch("trading.engine.time.time", side_effect=lambda: self.now)
        clock.start()
        self.addCleanup(clock.stop)
        env = patch.dict(os.environ, {"FEISHU_EVENT_WEBHOOK_URL": "https://open.feishu.cn/open-apis/bot/v2/hook/test-position",
            "FEISHU_SCHEDULED_WEBHOOK_URL": "", "ASTER_CAPACITY_ALERT_ENABLED": "0"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        sender = patch("monitor.send_feishu")
        self.sender = sender.start()
        self.addCleanup(sender.stop)

    def publish(self, long="2", short="1"):
        self.engine.view("test", snapshot=snapshot(self.now, long, short))

    def notify(self, advance=0, long="2", short="1"):
        self.now += advance
        self.publish(long, short)
        self.engine.notify()
        self.assertIsNone(self.engine.position_imbalance_error)

    def collect(self):
        return observations.collect(self.engine)

    def pair(self):
        other = account("other", mode="live")
        other["enabled"] = False
        self.f.store.save_account(other)
        pair = {"id": "pair", "name": "测试配对", "symbol": "XAUUSD1", "long_account_id": "test", "short_account_id": "other",
                "enabled": False}
        with self.f.store.connect() as db:
            db.execute("INSERT INTO pairs VALUES (?,?)", (pair["id"], dumps(pair)))
            db.executemany("INSERT INTO pair_members VALUES (?,?,?)", [("test", "pair", "LONG"), ("other", "pair", "SHORT")])
        return pair

    def pair_state(self, **kwargs):
        state = {"phase": "paused", "pending": None, "snapshots": {"long": snapshot(self.now, "2", "0"),
                  "short": snapshot(self.now, "0", "1")}, **kwargs}
        self.f.store.put("pair_runtime:pair", state)
        return state

    def test_stable_fault_and_recovery_use_event_robot_and_no_exchange_reads(self):
        with ExitStack() as stack:
            calls = [stack.enter_context(patch.object(self.f.market, name, side_effect=AssertionError("unexpected exchange read")))
                     for name in ("load_rules", "book", "mark_price", "capacities", "depth")]
            calls += [stack.enter_context(patch.object(self.engine, "broker", side_effect=AssertionError("broker acquisition"))),
                      stack.enter_context(patch.object(self.engine, "state", side_effect=AssertionError("full dashboard report")))]
            self.notify()
            self.notify(60)
            self.notify(60)
            self.assertEqual(self.sender.call_count, 1)
            self.assertIn("仓位不平衡", self.sender.call_args.args[1])
            self.notify(60, "1", "1")
            self.notify(60, "1", "1")
            self.assertEqual(self.sender.call_count, 2)
            self.assertIn("恢复", self.sender.call_args.args[1])
            for call in calls:
                call.assert_not_called()

    def test_transient_intents_and_post_fill_suppress_even_paused_accounts(self):
        for kind in ("pair", "cycle", "migration", "leverage", "cycle_leverage"):
            for status in ("pending", "repair", "attention"):
                item = pending(status, kind)
                item["receipts"] = {}  # attention with uncertain orders is still reconciliation
                self.f.store.save_intent(item)
                self.publish()
                rows = self.collect()
                self.assertTrue(all(r["suppressed"] for r in rows), (kind, status))
        self.f.store.save_intent({**item, "status": "aborted"})
        self.f.store.put("post_fill_check:test", {"symbol": "XAUUSD1"})
        self.assertTrue(all(r["suppressed"] for r in self.collect()))

    def test_terminal_attention_is_observed_but_renewed_repair_is_not(self):
        self.f.store.save_intent(pending("attention"))
        self.notify()
        self.notify(60)
        self.sender.assert_called_once()
        self.assertIn("自动处理已暂停", self.sender.call_args.args[1])
        self.f.store.save_intent(pending("repair"))
        self.notify(60, "1", "1")
        self.notify(60, "1", "1")
        self.sender.assert_called_once()

    def test_fast_trade_between_notification_ticks_restarts_confirmation(self):
        self.notify()
        item = pending()
        self.f.store.save_intent(item)
        # Completion uses direct SQL in several execution paths.
        with self.f.store.connect() as db:
            db.execute("UPDATE intents SET status='complete',data=? WHERE id=?", (dumps({**item, "status": "complete"}), item["id"]))
        self.notify(60)
        self.sender.assert_not_called()
        self.notify(60)
        self.sender.assert_called_once()

    def test_pair_members_are_one_subject_and_active_repairs_suppress(self):
        self.pair()
        self.pair_state()
        self.assertEqual(len(self.collect()), 1)
        self.engine.notify()
        self.now += 60
        self.pair_state()
        self.engine.notify()
        self.sender.assert_called_once()
        self.assertIn("配对组", self.sender.call_args.args[1])
        intent = pending("attention")
        legs = [{"order": intent["orders"][0], "receipt": intent["receipts"]["leg"]}]
        batch = {"id": "batch", "legs": legs, "repairs": []}
        self.pair_state(pending=batch, phase="attention")
        self.assertFalse(self.collect()[0]["suppressed"])
        self.pair_state(pending=batch, phase="repairing", attention="old attention text")
        self.assertTrue(self.collect()[0]["suppressed"])
        self.pair_state(recovery_watch={"batches": [{"legs": legs}]})
        self.assertTrue(self.collect()[0]["suppressed"])

    def test_fast_pair_transaction_is_seen_without_history_scan(self):
        self.pair()
        self.pair_state()
        self.engine.notify()
        self.pair_state(phase="submitting", pending={"id": "batch"})
        self.pair_state()
        self.now += 60
        self.pair_state()
        self.engine.notify()
        self.sender.assert_not_called()
        self.now += 60
        self.pair_state()
        self.engine.notify()
        self.sender.assert_called_once()

    def test_pair_reverse_positions_are_included_in_actual_totals(self):
        self.pair()
        for offset in (0, 60):
            self.now += offset
            self.pair_state(snapshots={"long": snapshot(self.now, "10", "3"), "short": snapshot(self.now, "0", "10")})
            self.engine.notify()
        self.sender.assert_called_once()
        row = self.collect()[0]
        self.assertEqual((row["long_qty"], row["short_qty"]), ("10", "13"))

    def test_no_fresh_pair_evidence_never_advances_confirmation(self):
        self.pair()
        self.pair_state()
        self.engine.notify()
        self.now += 60
        self.engine.notify()
        self.sender.assert_not_called()

    def test_final_sql_check_catches_a_trade_after_observation(self):
        self.notify()
        self.now += 60
        self.publish()
        self.engine.observe_position_imbalance(self.engine.notification_config("event"))
        queued = self.f.store.due_notifications()[0]
        self.f.store.save_intent(pending())
        self.assertIsNone(self.f.store.notification_for_delivery(queued["id"]))

    def test_existing_hot_cache_can_supply_evidence_but_revoked_lease_cannot(self):
        account_state = self.f.store.account("test")
        account_state["cycle"]["enabled"] = True
        self.f.store.save_account(account_state)
        broker = object.__new__(LiveBroker)
        evidence = snapshot(self.now)
        lease = Mock(snapshot=SimpleNamespace(timestamp=self.now, open_orders=[],
            positions=[SimpleNamespace(**p) for p in evidence["positions"]]))
        broker.cycle_cache = Mock()
        broker.cycle_cache.lease.return_value = lease
        self.engine.brokers["test"] = broker
        row = next(r for r in self.collect() if r["symbol"] == "XAUUSD1")
        self.assertEqual((row["long_qty"], row["short_qty"]), ("2", "1"))
        lease.require_fresh.side_effect = TradingError("revoked")
        row = next(r for r in self.collect() if r["symbol"] == "XAUUSD1")
        self.assertIsNone(row["long_qty"])

    def test_external_open_orders_suppress_and_missing_positions_are_not_zero(self):
        self.publish()
        self.engine.views["test"]["snapshot"]["open_orders"] = [{"status": "NEW"}]
        self.assertTrue(next(r for r in self.collect() if r["symbol"] == "XAUUSD1")["suppressed"])
        self.engine.views["test"]["snapshot"]["open_orders"] = []
        self.engine.views["test"]["snapshot"]["positions"].pop()
        self.engine.notify()
        self.now += 60
        self.engine.notify()
        self.sender.assert_not_called()

    def test_busy_worker_is_skipped_without_waiting(self):
        acquired, release = threading.Event(), threading.Event()
        def worker():
            with self.engine.account_lock("test"):
                acquired.set()
                release.wait(5)
        thread = threading.Thread(target=worker)
        thread.start()
        try:
            self.assertTrue(acquired.wait(2))
            self.assertTrue(all(not r["sample_times"] for r in self.collect()))
        finally:
            release.set()
            thread.join(2)

    def test_in_transaction_recheck_rejects_execution_after_capture(self):
        self.publish()
        rows = self.collect()
        self.f.store.save_intent(pending())
        with self.f.store.connect() as db:
            self.assertTrue(all(not observations.current(db, row) for row in rows))

    def test_activity_is_transactional_and_initialization_idempotent(self):
        self.f.store.save_intent(pending())
        initial = observations.activity(self.f.store, "account:test")
        with self.assertRaises(RuntimeError):
            with self.f.store.connect() as db:
                db.execute("UPDATE intents SET status='repair' WHERE id='intent'")
                raise RuntimeError("rollback")
        self.assertEqual(observations.activity(self.f.store, "account:test"), initial)
        self.assertEqual(observations.activity(Store(self.f.store.path), "account:test"), initial)

    def test_category_policy_is_independent_of_public_monitoring(self):
        config = self.f.store.edit_monitoring({"monitoring_enabled": False})
        self.assertTrue(monitoring.allowed(config, "position_imbalance", ["XAUUSD1"]))
        config = self.f.store.edit_monitoring({"alerts": False}, symbol="XAUUSD1")
        self.assertFalse(monitoring.allowed(config, "position_imbalance", ["XAUUSD1"]))

    def test_delivery_reobserves_after_preceding_network_send(self):
        self.notify()
        self.now += 60
        self.publish()
        with self.f.store.connect() as db:
            db.execute("INSERT INTO outbox(id,message,due_at,category,symbols) VALUES ('first','trade',0,'trade_summary','[]')")
        self.sender.side_effect = lambda *_: self.f.store.save_intent(pending())
        self.engine.notify()
        self.sender.assert_called_once()
        self.assertEqual(self.sender.call_args.args[1], "trade")


if __name__ == "__main__":
    unittest.main()
