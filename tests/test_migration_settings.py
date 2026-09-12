"""Per-account migration settings, opt-in behavior and durable defaults."""
import copy
import json
import os
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from trading.engine import Engine
from trading.migration import DEFAULT_MIGRATION
from trading.server import create_app
from trading.store import Store, dumps
from tests.helpers import Fixture, account


class MigrationSettingsTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.account["enabled"] = False
        self.f.store.save_account(self.f.account)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        password = "test-only-migration-settings-password"
        environment = patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": password}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.client = TestClient(create_app(self.engine, start_engine=False))
        self.addCleanup(self.client.close)
        self.client.headers["origin"] = "http://testserver"
        self.assertEqual(self.client.post("/api/login", json={"password": password}).status_code, 200)

    def update(self, migration, **policy):
        return self.client.patch("/api/accounts/test", json={"migration": migration, **policy})

    def test_new_accounts_default_off_and_saving_opt_in_does_not_execute_or_enable(self):
        response = self.client.post("/api/accounts", json={"id": "second", "name": "第二账户",
                                                         "mode": "paper", "env_prefix": "ASTER_SECOND"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.f.store.account("second")["migration"], DEFAULT_MIGRATION)
        before = copy.deepcopy(self.f.broker.state)
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("save must not submit")), \
             patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("save must not change leverage")):
            response = self.update({"enabled": True})
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.f.store.account("test")
        self.assertTrue(saved["migration"]["enabled"])
        self.assertFalse(saved["enabled"])
        self.assertNotIn("ASTER_ALLOW_LIVE", os.environ)
        self.assertEqual(self.f.broker.state, before)
        self.assertEqual(self.f.store.pending_notifications(), 0)

    def test_partial_migration_and_policy_updates_preserve_other_fields(self):
        original = self.f.store.account("test")
        response = self.update({"spread_limit_bp": "3.5"}, threshold="20000")
        self.assertEqual(response.status_code, 200, response.text)
        response = self.update({"batch_notional": "500", "notional_tolerance": "0"})
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.f.store.account("test")
        self.assertEqual(saved["migration"], {**DEFAULT_MIGRATION, "spread_limit_bp": "3.5",
                                              "batch_notional": "500", "notional_tolerance": "0"})
        self.assertEqual(saved["policy"], {**original["policy"], "threshold": "20000"})

    def test_invalid_nested_values_cannot_partially_persist_other_settings(self):
        invalid = [None, {}, [], "enabled", {"unknown": "1"}, {"enabled": None},
                   {"spread_limit_bp": "0"}, {"spread_limit_bp": "100.00001"},
                   {"batch_notional": "0"}, {"batch_notional": "499.999"}, {"batch_notional": "1000001"},
                   {"notional_tolerance": "-0.01"}, {"notional_tolerance": "0.5000001"}]
        invalid += [{"enabled": value} for value in (0, 1, "true", "false", [], {})]
        invalid += [{field: value} for field in ("spread_limit_bp", "batch_notional", "notional_tolerance")
                    for value in (None, True, 1, 0.05, "NaN", "Infinity", "")]
        original = self.f.store.account("test")
        for migration in invalid:
            with self.subTest(migration=migration):
                response = self.update(migration, threshold="99999")
                self.assertIn(response.status_code, (409, 422), response.text)
                self.assertEqual(self.f.store.account("test"), original)

    def test_bad_policy_cannot_persist_valid_migration_or_new_run_id(self):
        original = self.f.store.account("test")
        response = self.update({"enabled": True}, threshold="NaN")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.f.store.account("test"), original)

    def test_running_and_pending_accounts_cannot_change_migration(self):
        for running in (True, False):
            saved = self.f.store.account("test")
            saved["enabled"] = running
            self.f.store.save_account(saved)
            if not running:
                self.f.store.save_intent({"id": "unresolved-migration", "account_id": "test",
                                          "kind": "migration", "status": "attention"})
            before = self.f.store.account("test")
            response = self.update({"enabled": True})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(self.f.store.account("test"), before)

    def test_stop_during_pending_batch_preserves_intent_ledger_and_reconciliation(self):
        saved = self.f.store.account("test")
        saved.update(enabled=True, migration={**DEFAULT_MIGRATION, "enabled": True}, migration_run_id="keep-run")
        self.f.store.save_account(saved)
        intent = {"id": "already-submitted", "account_id": "test", "kind": "migration",
                  "status": "pending", "symbol": "XAUUSD1", "target_symbol": "SPCXUSD1",
                  "phase": "close_source", "orders": {"source_long": {"client_id": "keep-client-id"}}}
        ledger = {"run_id": "keep-run", "completed_batches": 2, "migrated_notional": {"LONG": "2000", "SHORT": "2001"}}
        self.f.store.save_intent(intent)
        self.f.store.put("migration:test", ledger)
        response = self.update({"enabled": False})
        self.assertEqual(response.status_code, 200, response.text)
        after = self.f.store.account("test")
        self.assertEqual(after, {**saved, "enabled": False, "migration": {**saved["migration"], "enabled": False}})
        self.assertEqual(self.f.store.intent("test"), intent)
        self.assertEqual(self.f.store.get("migration:test"), ledger)
        self.assertEqual(self.engine.views["test"]["status"], "reconciling")
        self.assertIn("test", self.engine.wake_accounts)
        self.assertIn("test", self.engine.urgent_accounts)
        with patch("trading.engine.MigrationExecutor.reconcile", return_value="继续核对已提交减仓") as reconcile:
            self.engine.tick_account("test")
        reconcile.assert_called_once()
        self.assertFalse(reconcile.call_args.args[0]["enabled"])
        self.assertFalse(reconcile.call_args.args[0]["migration"]["enabled"])

    def test_stopping_running_migration_also_pauses_ordinary_additions(self):
        saved = self.f.store.account("test")
        saved.update(enabled=True, migration={**DEFAULT_MIGRATION, "enabled": True}, migration_run_id="keep-run")
        self.f.store.save_account(saved)
        before = copy.deepcopy(self.f.broker.state)
        response = self.update({"enabled": False})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.engine.views["test"]["status"], "paused")
        self.assertEqual(self.f.store.account("test")["migration_run_id"], "keep-run")
        with patch("trading.engine.Executor.open_pair", side_effect=AssertionError("ordinary additions must remain paused")), \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("stop must not submit")):
            self.engine.tick_account("test")
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.f.broker.state, before)

    def test_stop_exception_rejects_additional_fields_and_non_boolean_false(self):
        for running in (True, False):
            saved = self.f.store.account("test")
            saved.update(enabled=running, migration={**DEFAULT_MIGRATION, "enabled": True})
            self.f.store.save_account(saved)
            if not running:
                self.f.store.save_intent({"id": "unresolved-stop", "account_id": "test",
                                          "kind": "migration", "status": "pending"})
            for changes in ({"migration": {"enabled": False, "batch_notional": "2000"}},
                            {"migration": {"enabled": False}, "threshold": "30000"},
                            {"migration": {"enabled": 0}}, {"migration": {"enabled": "false"}}):
                with self.subTest(running=running, changes=changes):
                    response = self.client.patch("/api/accounts/test", json=changes)
                    self.assertIn(response.status_code, (409, 422), response.text)
                    self.assertEqual(self.f.store.account("test"), saved)

    def test_run_id_changes_only_when_opted_back_in(self):
        self.assertEqual(self.update({"enabled": True}).status_code, 200)
        first = self.f.store.account("test")["migration_run_id"]
        self.assertTrue(first)
        self.assertEqual(self.update({"spread_limit_bp": "4"}).status_code, 200)
        self.assertEqual(self.f.store.account("test")["migration_run_id"], first)
        self.assertEqual(self.update({"enabled": True}).status_code, 200)
        self.assertEqual(self.f.store.account("test")["migration_run_id"], first)
        self.assertEqual(self.update({"enabled": False}).status_code, 200)
        self.assertEqual(self.f.store.account("test")["migration_run_id"], first)
        self.assertEqual(self.update({"enabled": True}).status_code, 200)
        self.assertNotEqual(self.f.store.account("test")["migration_run_id"], first)

    def test_legacy_accounts_gain_independent_off_defaults_without_pausing(self):
        for account_id, enabled in (("legacy_running", True), ("legacy_paused", False)):
            legacy = account(account_id)
            legacy.update(enabled=enabled)
            legacy["policy"]["margin_limit"] = "0.7"
            legacy["policy"].pop("min_open_leverage")
            with self.f.store.connect() as db:
                db.execute("INSERT INTO accounts VALUES (?,?)", (account_id, dumps(legacy)))
        restarted = Store(self.f.store.path)
        first, second = (restarted.account(name) for name in ("legacy_running", "legacy_paused"))
        self.assertTrue(first["enabled"])
        self.assertFalse(second["enabled"])
        for saved in (first, second):
            self.assertEqual(saved["migration"], DEFAULT_MIGRATION)
            self.assertEqual(saved["policy"]["margin_limit"], "0.7")
            self.assertEqual(saved["policy"]["min_open_leverage"], 5)
            with restarted.connect() as db:
                raw, = db.execute("SELECT data FROM accounts WHERE id=?", (saved["id"],)).fetchone()
            self.assertEqual(json.loads(raw)["migration"], DEFAULT_MIGRATION)
        first["migration"]["enabled"] = True
        self.assertFalse(second["migration"]["enabled"])
        self.assertFalse(DEFAULT_MIGRATION["enabled"])

    def test_settings_and_run_id_survive_restart_and_do_not_affect_another_account(self):
        self.f.store.save_account(account("second"))
        response = self.update({"enabled": True, "spread_limit_bp": "2.5", "notional_tolerance": "0.03"})
        self.assertEqual(response.status_code, 200, response.text)
        before = self.f.store.account("test")
        restarted = Store(self.f.store.path)
        states = {item["id"]: item for item in Engine(restarted, market=self.f.market).state()["accounts"]}
        self.assertEqual(states["test"]["migration"], before["migration"])
        self.assertEqual(states["test"]["migration_run_id"], before["migration_run_id"])
        self.assertEqual(states["second"]["migration"], DEFAULT_MIGRATION)


if __name__ == "__main__":
    unittest.main()
