"""Exercise durable migration handoffs through the real account worker."""
from unittest import TestCase
from unittest.mock import patch

from trading.engine import Engine
from trading.exchange import AmbiguousOrder, ExchangeError
from trading.migration import DEFAULT_MIGRATION
from trading.models import SYMBOLS, dec, wire
from trading.paper import PaperBroker
from trading.store import Store
from .helpers import Fixture, account


SOURCE, TARGET = "XAUUSD1", "SPCXUSD1"


class MigrationRecoveryIntegrationTests(TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        saved = self.f.store.account("test")
        saved.update(migration={**DEFAULT_MIGRATION, "enabled": True}, migration_run_id="recovery-run")
        self.f.store.save_account(saved)
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"][SOURCE + ":" + side] = {
                "qty": "1", "entry": wire(self.f.market.book(SOURCE).mark)}
        self.f.broker.save()
        self.engine = Engine(self.f.store, demo=True, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        for symbol in SYMBOLS:
            self.engine.poll_market(symbol)

    def pending_target(self):
        """The exchange committed both target fills but their replies were lost."""
        submit = self.f.broker.submit
        def accepted(orders):
            self.assertEqual({order["symbol"] for order in orders}, {TARGET})
            submit(orders)
            raise AmbiguousOrder("lost target acknowledgement")
        with patch.object(self.f.broker, "submit", side_effect=accepted) as writer, \
             patch.object(self.f.broker, "query", side_effect=ExchangeError("not visible yet", code=-2013)), \
             patch("trading.engine.Executor.open_pair", side_effect=AssertionError("ordinary entry forbidden")):
            self.engine.tick_account("test")
        self.assertEqual(writer.call_count, 1)
        intent = self.f.store.intent("test")
        self.assertEqual((intent["kind"], intent["phase"]), ("migration", "open_target"))
        self.assertEqual([p.qty for p in self.f.broker.snapshot(SYMBOLS).pair(SOURCE)], [1, 1])
        self.assertTrue(all(p.qty > 0 for p in self.f.broker.snapshot(SYMBOLS).pair(TARGET)))
        return {order["newClientOrderId"] for order in intent["orders"]}

    def restart(self):
        store = Store(self.f.store.path)
        broker = PaperBroker("test", self.f.market, store)
        engine = Engine(store, demo=True, market=self.f.market)
        engine.brokers["test"] = broker
        return store, broker, engine

    def assert_recovery_reductions(self, writer, original_ids):
        self.assertGreater(writer.call_count, 0)
        for call in writer.call_args_list:
            for order in call.args[0]:
                self.assertNotIn(order["newClientOrderId"], original_ids)
                self.assertEqual(order["side"], "SELL" if order["positionSide"] == "LONG" else "BUY")

    def test_restart_with_paused_account_and_disabled_migration_finishes_only_committed_batch(self):
        target_ids = self.pending_target()
        self.engine.enable("test", False)
        # Simulate the saved off switch present at startup. The settings UI
        # separately disallows editing other settings while a batch is pending.
        saved = self.f.store.account("test")
        saved["migration"]["enabled"] = False
        self.f.store.save_account(saved)
        store, broker, engine = self.restart()
        with patch.object(broker, "submit", wraps=broker.submit) as writer, \
             patch("trading.engine.Executor.open_pair", side_effect=AssertionError("ordinary entry forbidden")):
            engine.tick_account("test")
            count = writer.call_count
            engine.tick_account("test")
            self.assertEqual(writer.call_count, count)
        self.assert_recovery_reductions(writer, target_ids)
        self.assertIsNone(store.intent("test"))
        self.assertEqual(store.get("migration:test")["completed_batches"], 1)
        self.assertFalse(store.account("test")["enabled"])
        self.assertFalse(store.account("test")["migration"]["enabled"])
        self.assertTrue(all(p.qty < 1 for p in broker.snapshot(SYMBOLS).pair(SOURCE)))

    def test_capacity_disappearing_after_target_fills_does_not_block_recovery_or_start_another_batch(self):
        target_ids = self.pending_target()
        store, broker, engine = self.restart()
        with patch.object(self.f.market, "capacities", return_value={5: dec(0), 10: dec(0), 20: dec(0)}):
            for symbol in SYMBOLS:
                engine.poll_market(symbol)
        with patch.object(broker, "submit", wraps=broker.submit) as writer, \
             patch("trading.engine.Executor.open_pair", side_effect=AssertionError("ordinary entry forbidden")):
            engine.tick_account("test")
            count = writer.call_count
            engine.tick_account("test")
            self.assertEqual(writer.call_count, count)
        self.assert_recovery_reductions(writer, target_ids)
        self.assertIsNone(store.intent("test"))
        self.assertEqual(store.get("migration:test")["completed_batches"], 1)
        self.assertTrue(store.account("test")["enabled"])
        self.assertIn("5x", engine.state()["accounts"][0]["migration_state"]["reason"])

    def test_completed_batch_risk_check_survives_crash_and_pauses_before_any_new_order(self):
        complete = self.f.store.complete_migration
        def commit_then_stop(intent, result, remaining):
            complete(intent, result, remaining)
            raise RuntimeError("process stopped after completion commit")
        with patch.object(self.f.store, "complete_migration", side_effect=commit_then_stop), \
             self.assertRaisesRegex(RuntimeError, "completion commit"):
            self.engine.tick_account("test")
        self.assertIsNone(self.f.store.intent("test"))
        self.assertTrue(self.f.store.get("post_fill_check:test"))
        self.assertTrue(self.f.store.account("test")["enabled"])
        # A later equity debit makes the persisted check actionable on restart.
        self.f.broker.reload()
        self.f.broker.state["wallet"] = "2000"
        self.f.broker.save()
        store, broker, engine = self.restart()
        with patch.object(broker, "submit", side_effect=AssertionError("risk check precedes every new order")), \
             patch("trading.engine.Executor.open_pair", side_effect=AssertionError("ordinary entry forbidden")):
            engine.tick_account("test")
        self.assertFalse(store.account("test")["enabled"])
        self.assertIn("保证金占用率超过上限", store.account("test")["pause_reason"])
        self.assertIsNone(store.get("post_fill_check:test"))
        self.assertEqual(store.get("migration:test")["completed_batches"], 1)

    def test_restart_after_completion_preserves_five_x_migration_headroom(self):
        saved = self.f.store.account("test")
        saved["policy"]["margin_limit"] = ".93"
        self.f.store.save_account(saved)
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"][SOURCE + ":" + side]["qty"] = "13.316"
        self.f.broker.save()
        complete = self.f.store.complete_migration

        def commit_then_stop(intent, result, remaining):
            complete(intent, result, remaining)
            raise RuntimeError("process stopped after completion commit")

        with patch.object(self.f.store, "complete_migration", side_effect=commit_then_stop), \
             self.assertRaisesRegex(RuntimeError, "completion commit"):
            self.engine.tick_account("test")
        marker = {"kind": "migration", "symbol": TARGET, "leverage": 5}
        self.assertEqual(self.f.store.get("post_fill_check:test"), marker)
        store, broker, engine = self.restart()
        snapshot = broker.snapshot(SYMBOLS)
        self.assertGreater(snapshot.ratio, dec(".93"))
        self.assertFalse(snapshot.margin_exceeds(".98"))
        # No new capacity has been polled. Only the durable post-fill check runs,
        # and the completed migration must not fall back to the ordinary 5x cap.
        with patch.object(broker, "submit", side_effect=AssertionError("no new capacity")):
            engine.tick_account("test")
        self.assertTrue(store.account("test")["enabled"])
        self.assertNotIn("pause_reason", store.account("test"))
        self.assertIsNone(store.get("post_fill_check:test"))
        self.assertEqual(store.get("migration:test")["completed_batches"], 1)

    def test_five_x_capacity_edge_below_ordinary_threshold_wakes_only_once(self):
        ordinary = account("ordinary")
        ordinary["policy"]["symbols"] = [TARGET]
        self.f.store.save_account(ordinary)
        self.engine = Engine(self.f.store, demo=True, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        def publish(symbol, amount):
            with patch.object(self.f.market, "capacities", return_value={5: dec(amount), 10: dec(0), 20: dec(0)}):
                self.engine.poll_market(symbol)
        publish("CLUSD1", 0)
        publish(TARGET, 0)
        self.assertEqual(self.engine.priority_accounts, {})
        self.assertEqual(self.f.store.account("test")["policy"]["threshold"], "10000")
        publish(TARGET, 2000)
        self.assertEqual(set(self.engine.priority_accounts), {"test"})
        self.assertTrue(self.engine.scheduler_event.is_set())
        # Consume the published opportunity at the scheduler/worker boundary.
        with self.engine.lock:
            signal = self.engine.priority_accounts.pop("test")
            self.engine.active_priority_accounts.add("test")
            self.engine.active_priority_signals["test"] = signal
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as writer, \
             patch("trading.engine.Executor.open_pair", side_effect=AssertionError("ordinary entry forbidden")):
            self.engine.tick_account("test")
            self.assertGreater(writer.call_count, 0)
            count = writer.call_count
            self.engine.scheduler_event.clear()
            publish(TARGET, 1999)
            publish(TARGET, 2100)
            self.assertEqual(writer.call_count, count)
        self.assertEqual(self.engine.priority_accounts, {})
        self.assertFalse(self.engine.scheduler_event.is_set())
        self.assertEqual(self.f.store.get("migration:test")["completed_batches"], 1)

    def test_migration_leverage_confirmation_after_restart_never_schedules_ordinary_first_add(self):
        self.f.broker.state["leverages"][SOURCE] = 10
        self.f.broker.save()
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("upgrade first")):
            self.engine.tick_account("test")
        intent = self.f.store.intent("test")
        self.assertEqual((intent["kind"], intent["purpose"], intent["target"]), ("leverage", "migration", 10))
        store, broker, engine = self.restart()
        with patch.object(broker, "set_leverage", side_effect=AssertionError("confirmed durable leverage must not be resent")), \
             patch.object(broker, "submit", side_effect=AssertionError("confirmation is a separate worker step")):
            engine.tick_account("test")
        self.assertIsNone(store.intent("test"))
        self.assertEqual(broker.snapshot(SYMBOLS).pair(TARGET)[0].leverage, 10)
        self.assertIsNone(store.get("open_after_leverage:test:" + TARGET))
        self.assertEqual(broker.state["orders"], {})
        for symbol in SYMBOLS:
            engine.poll_market(symbol)
        with patch("trading.engine.Executor.open_pair", side_effect=AssertionError("ordinary entry forbidden")):
            engine.tick_account("test")
        self.assertEqual(store.get("migration:test")["completed_batches"], 1)
        self.assertIsNone(store.get("open_after_leverage:test:" + TARGET))
        self.assertEqual(broker.snapshot(SYMBOLS).pair(TARGET)[0].leverage, 10)
