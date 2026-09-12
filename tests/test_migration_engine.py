from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from tests.helpers import Fixture
from trading.engine import Engine
from trading.migration import DEFAULT_MIGRATION
from trading.models import SYMBOLS, dec, wire


class MigrationEngineTests(TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        account = self.f.store.account("test")
        account.update(migration={**DEFAULT_MIGRATION, "enabled": True}, migration_run_id="test-run")
        self.f.store.save_account(account)
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"]["XAUUSD1:" + side] = {"qty": "1", "entry": wire(self.f.market.book("XAUUSD1").mark)}
        self.save_paper()
        self.engine = Engine(self.f.store, demo=True, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.refresh()

    def save_paper(self):
        self.f.store.put("paper:test", self.f.broker.state)

    def refresh(self):
        for symbol in SYMBOLS:
            self.engine.poll_market(symbol)

    def tick(self):
        self.refresh()
        return self.engine.tick_account("test")

    def test_migrates_source_to_zero_and_keeps_ordinary_additions_disabled(self):
        with patch("trading.engine.Executor.open_pair", side_effect=AssertionError("ordinary additions forbidden")):
            for _ in range(35):
                self.tick()
                if self.engine.state()["accounts"][0]["migration_state"]["phase"] == "complete":
                    break
            for _ in range(3):
                self.tick()
        remaining = self.f.broker.snapshot(SYMBOLS).pair("XAUUSD1")
        self.assertEqual([p.qty for p in remaining], [dec(0), dec(0)])
        self.assertIsNone(self.f.store.intent("test"))
        progress = self.f.store.get("migration:test")
        self.assertGreater(progress["completed_batches"], 0)
        self.assertEqual(progress["phase"], "complete")
        self.assertIsNone(self.f.store.get("campaign:test"))
        for side in ("LONG", "SHORT"):
            self.assertLessEqual(abs(dec(progress["cumulative_notional_delta"][side])), dec(progress["migrated_notional"][side]) * dec("0.05"))

    def test_narrowest_target_and_fallback_require_five_x_capacity(self):
        self.tick()
        self.assertEqual(self.f.store.get("migration:test")["target_symbol"], "SPCXUSD1")
        state = self.engine.state()["accounts"][0]["migration_state"]
        self.assertEqual((state["required_leverage"], state["target_leverage"]), (5, 5))
        self.engine.markets["SPCXUSD1"]["capacities"]["5"] = "0"
        self.engine.tick_account("test")
        self.assertEqual(self.f.store.get("migration:test")["target_symbol"], "CLUSD1")

    def test_upgrade_is_durable_and_never_schedules_ordinary_first_add(self):
        self.f.broker.state["leverages"]["XAUUSD1"] = 10
        self.save_paper()
        self.tick()
        pending = self.f.store.intent("test")
        self.assertEqual((pending["kind"], pending["purpose"], pending["target"]), ("leverage", "migration", 10))
        self.assertEqual(len(self.f.broker.state["orders"]), 0)
        self.tick()
        self.assertIsNone(self.f.store.intent("test"))
        self.assertIsNone(self.f.store.get("open_after_leverage:test:SPCXUSD1"))
        self.tick()
        self.assertGreater(len(self.f.broker.state["orders"]), 0)
        self.assertEqual(self.f.store.get("migration:test")["source_leverage"], 10)

    def test_no_five_x_capacity_waits_without_ordinary_orders(self):
        for symbol in ("SPCXUSD1", "CLUSD1"):
            self.engine.markets[symbol]["capacities"]["5"] = "0"
        self.engine.tick_account("test")
        self.assertEqual(self.f.broker.state["orders"], {})
        state = self.engine.state()["accounts"][0]["migration_state"]
        self.assertEqual(state["phase"], "waiting")
        self.assertIn("5x", state["reason"])

    def test_full_margin_waits_and_does_not_credit_unfilled_source_reduction(self):
        account = self.f.store.account("test")
        account["policy"]["margin_limit"] = "1"
        self.f.store.save_account(account)
        self.f.broker.state["wallet"] = "1764.806"
        self.save_paper()
        self.tick()
        self.assertEqual(self.f.broker.state["orders"], {})
        self.assertEqual(self.engine.state()["accounts"][0]["migration_state"]["phase"], "waiting")

    def test_manual_source_change_pauses_and_does_not_count_as_migration(self):
        snapshot = self.f.broker.snapshot(SYMBOLS)
        self.engine.migration_progress(self.f.store.account("test"), snapshot)
        self.f.broker.state["positions"]["XAUUSD1:LONG"]["qty"] = "0.9"
        self.save_paper()
        self.tick()
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.f.broker.state["orders"], {})
        self.assertEqual(self.f.store.get("migration:test")["migrated_notional"], {"LONG": "0", "SHORT": "0"})

    def test_start_checks_all_migration_symbols_even_with_source_only_policy(self):
        self.engine.enable("test", False)
        snapshot = self.f.broker.snapshot(SYMBOLS)
        snapshot.positions = [p for p in snapshot.positions if (p.symbol, p.side) != ("CLUSD1", "SHORT")]
        with patch.object(self.f.broker, "snapshot", return_value=snapshot) as reader:
            with self.assertRaisesRegex(Exception, "缺少双向持仓信息"):
                self.engine.enable("test", True)
        self.assertEqual(set(reader.call_args.args[0]), set(SYMBOLS))
        self.assertFalse(self.f.store.account("test")["enabled"])

    def test_restart_preserves_progress_and_detects_external_change(self):
        self.tick()
        before = deepcopy(self.f.store.get("migration:test"))
        restarted = Engine(self.f.store, demo=True, market=self.f.market)
        restarted.brokers["test"] = self.f.broker
        for symbol in SYMBOLS:
            restarted.poll_market(symbol)
        restarted.tick_account("test")
        after = self.f.store.get("migration:test")
        self.assertEqual(after["run_id"], before["run_id"])
        self.assertGreater(after["completed_batches"], before["completed_batches"])

    def test_completed_store_transaction_is_idempotent(self):
        self.tick()
        with self.f.store.connect() as db:
            import json
            intent = json.loads(db.execute("SELECT data FROM intents WHERE account_id='test' AND status='complete'").fetchone()[0])
        before = self.f.store.get("migration:test")
        self.f.store.complete_migration(intent, intent["result"], before["source_remaining_qty"])
        self.assertEqual(self.f.store.get("migration:test"), before)

    def test_failed_progress_commit_keeps_intent_and_totals_recoverable(self):
        import sqlite3
        self.engine.migration_progress(self.f.store.account("test"), self.f.broker.snapshot(SYMBOLS))
        intent = {"id": "transaction", "account_id": "test", "kind": "migration", "run_id": "test-run",
                  "status": "pending", "target_symbol": "SPCXUSD1", "target_leverage": 5}
        self.f.store.save_intent(intent)
        with self.f.store.connect() as db:
            db.execute("""CREATE TRIGGER reject_migration_progress BEFORE UPDATE ON kv
                WHEN NEW.key='migration:test' BEGIN SELECT RAISE(ABORT, 'simulated disk failure'); END""")
        result = {key: {"LONG": value, "SHORT": value} for key, value in
                  (("source_qty", "0.2"), ("target_qty", "1.2"), ("source_notional", "880"), ("target_notional", "875"))}
        with self.assertRaises(sqlite3.DatabaseError):
            self.f.store.complete_migration(intent, result, {"LONG": "0.8", "SHORT": "0.8"})
        self.assertEqual(self.f.store.intent("test")["status"], "pending")
        self.assertEqual(self.f.store.get("migration:test")["migrated_notional"], {"LONG": "0", "SHORT": "0"})
        self.assertIsNone(self.f.store.get("post_fill_check:test"))
