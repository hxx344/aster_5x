"""An explicit review can retire mismatched tracking without issuing any orders."""
from copy import deepcopy
from dataclasses import replace
import os
import sqlite3
import time
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests import test_cycle_engine as engine_cases
from trading.cycle import CYCLE_POSITION_MISMATCH
from trading.engine import Engine
from trading.models import TradingError
from trading.server import create_app
from trading.store import Store


class CycleRecoveryTests(TestCase):
    select = engine_cases.CycleEngineTests.select
    start_holding = engine_cases.CycleEngineTests.start_holding

    def setUp(self):
        engine_cases.CycleEngineTests.setUp(self)
        self.previous = self.start_holding()
        self.f.broker.state["positions"]["XAUUSD1:LONG"]["qty"] = "0"
        self.f.broker.save()
        self.engine.tick_account("test")
        self.assertEqual(self.f.store.account("test")["pause_reason"], CYCLE_POSITION_MISMATCH)
        self.before_account = self.f.store.account("test")
        self.before_progress = self.f.store.get("cycle:test")
        self.before_orders = deepcopy(self.f.broker.state["orders"])

    def assert_unchanged(self):
        self.assertEqual(self.f.store.account("test"), self.before_account)
        self.assertEqual(self.f.store.get("cycle:test"), self.before_progress)
        self.assertEqual(self.f.broker.state["orders"], self.before_orders)

    def preview(self):
        return self.engine.preview_cycle_recovery("test")

    def confirm(self, preview):
        self.engine.confirm_cycle_recovery("test", preview["token"])

    def ledger(self):
        with self.f.store.connect() as db:
            return {table: [tuple(row) for row in db.execute("SELECT * FROM " + table)]
                    for table in ("intents", "cycle_fills", "cycle_symbol_volume_days", "cycle_volume_sync")}

    def test_preview_is_read_only_and_confirmation_preserves_positions_and_history_across_restart(self):
        ledger = self.ledger()
        actual = deepcopy(self.f.broker.state)
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not trade")), \
             patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("must not change leverage")), \
             patch.object(self.f.broker, "cycle_snapshot", wraps=self.f.broker.cycle_snapshot) as read:
            preview = self.preview()
            self.assert_unchanged()
            self.assertEqual(preview["actual"]["LONG"], "0")
            self.assertEqual(preview["expected"], self.previous["quantities"])
            self.confirm(preview)
        self.assertEqual(read.call_count, 2)
        self.assertTrue(all(call.kwargs["fresh_modes"] for call in read.call_args_list))
        self.assertEqual(self.f.broker.state, actual)
        self.assertEqual(self.ledger(), ledger)
        reopened = Store(self.f.store.path)
        account = reopened.account("test")
        progress = reopened.get("cycle:test")
        self.assertFalse(account["enabled"])
        self.assertNotIn("pause_reason", account)
        self.assertEqual(progress["baseline"], preview["actual"])
        self.assertEqual(progress["quantities"], {"LONG": "0", "SHORT": "0"})
        self.assertIsNone(progress["opened_at"])
        self.assertEqual(progress["completed_cycles"], self.previous["completed_cycles"])
        self.assertNotEqual(progress["run_id"], self.previous["run_id"])
        audit = reopened.get(f"cycle_recovery:test:{progress['run_id']}")
        self.assertEqual(audit["previous"], self.before_progress)
        self.assertEqual(audit["review"]["actual"], preview["actual"])
        self.assertTrue(any("已人工核对" in event["message"] for event in reopened.events()))
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("enable must not trade")):
            self.engine.enable("test", True)
        self.assertTrue(reopened.account("test")["enabled"])
        self.assertEqual(reopened.get("cycle:test")["baseline"], preview["actual"])

    def test_flat_actual_positions_can_be_acknowledged(self):
        self.f.broker.state["positions"]["XAUUSD1:SHORT"]["qty"] = "0"
        self.f.broker.save()
        preview = self.preview()
        self.confirm(preview)
        self.assertEqual(self.f.store.get("cycle:test")["baseline"], {"LONG": "0", "SHORT": "0"})
        self.engine.enable("test", True)

    def test_changed_quantity_or_leverage_requires_a_new_review(self):
        for change in ("quantity", "leverage"):
            with self.subTest(change=change):
                preview = self.preview()
                if change == "quantity":
                    self.f.broker.state["positions"]["XAUUSD1:LONG"]["qty"] = "0.001"
                else:
                    self.f.broker.state["leverages"]["XAUUSD1"] = 10
                self.f.broker.save()
                with self.assertRaisesRegex(TradingError, "已变化"):
                    self.confirm(preview)
                self.assert_unchanged()

    def test_changed_config_or_progress_cannot_be_acknowledged_from_old_preview(self):
        for target in ("account", "progress"):
            with self.subTest(target=target):
                preview = self.preview()
                if target == "account":
                    account = self.f.store.account("test")
                    account["name"] = "renamed"
                    self.f.store.save_account(account)
                else:
                    progress = self.f.store.get("cycle:test")
                    progress["completed_cycles"] += 1
                    self.f.store.put("cycle:test", progress)
                with self.assertRaisesRegex(TradingError, "已变化"):
                    self.confirm(preview)
                self.f.store.save_account(self.before_account)
                self.f.store.put("cycle:test", self.before_progress)
                self.assert_unchanged()

    def test_pending_batch_blocks_both_paths_before_reading_account(self):
        preview = self.preview()
        pending = {"id": "new-pending", "account_id": "test", "kind": "cycle", "status": "attention"}
        self.f.store.save_intent(pending)
        with patch.object(self.f.broker, "cycle_snapshot", side_effect=AssertionError("must reject before reads")):
            for run in (self.preview, lambda: self.confirm(preview)):
                with self.assertRaisesRegex(TradingError, "先核对未完成批次"):
                    run()
        self.assertEqual(self.f.store.intent("test"), pending)
        self.assert_unchanged()

    def test_expired_unknown_and_other_account_tokens_are_rejected_without_reads(self):
        preview = self.preview()
        self.engine.cycle_recovery_previews["test"]["expires"] = time.monotonic() - 1
        with patch.object(self.f.broker, "cycle_snapshot", side_effect=AssertionError("invalid token must not read")):
            for aid, token in (("test", preview["token"]), ("test", "a" * 32), ("other", preview["token"])):
                with self.assertRaisesRegex(TradingError, "已失效"):
                    self.engine.confirm_cycle_recovery(aid, token)
        self.assert_unchanged()

    def test_restarting_engine_invalidates_preview_and_replay_cannot_reset_again(self):
        preview = self.preview()
        restarted = Engine(self.f.store, market=self.f.market)
        with self.assertRaisesRegex(TradingError, "已失效"):
            restarted.confirm_cycle_recovery("test", preview["token"])
        self.assert_unchanged()
        self.confirm(preview)
        progress = self.f.store.get("cycle:test")
        with self.assertRaisesRegex(TradingError, "已失效"):
            self.confirm(preview)
        self.assertEqual(self.f.store.get("cycle:test"), progress)

    def test_stale_modes_permission_or_invalid_pair_block_confirmation(self):
        snapshot = self.f.broker.cycle_snapshot(["XAUUSD1"])
        for name, invalid in (
            ("stale", replace(snapshot, timestamp=time.time() - 30)),
            ("mode", replace(snapshot, hedge_mode=False)),
            ("permission", replace(snapshot, can_trade=False)),
            ("missing leg", replace(snapshot, positions=[])),
        ):
            with self.subTest(name=name):
                preview = self.preview()
                with patch.object(self.f.broker, "cycle_snapshot", return_value=invalid), self.assertRaises(TradingError):
                    self.confirm(preview)
                self.assert_unchanged()

    def test_only_position_mismatch_pause_offers_recovery(self):
        self.assertTrue(self.engine.state()["accounts"][0]["cycle_recovery_available"])
        for change in ({"enabled": True}, {"pause_reason": "账户模式错误"}, {"pause_reason": None}):
            self.f.store.save_account({**self.before_account, **change})
            with self.assertRaisesRegex(TradingError, "仅可确认"):
                self.preview()
            self.assertFalse(self.engine.state()["accounts"][0]["cycle_recovery_available"])

    def test_database_failure_rolls_back_archive_progress_account_and_event(self):
        preview = self.preview()
        with self.f.store.connect() as db:
            db.execute("CREATE TRIGGER reject_recovery BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT, 'test write failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.confirm(preview)
        self.assert_unchanged()
        with self.f.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM kv WHERE key LIKE 'cycle_recovery:%'").fetchone()[0], 0)

    def test_commit_rechecks_for_concurrent_pending_batch(self):
        preview = self.preview()
        commit = self.f.store.confirm_cycle_recovery
        def racing_commit(*args):
            self.f.store.save_intent({"id": "late-pending", "account_id": "test", "kind": "cycle", "status": "pending"})
            commit(*args)
        with patch.object(self.f.store, "confirm_cycle_recovery", side_effect=racing_commit), self.assertRaisesRegex(TradingError, "已变化"):
            self.confirm(preview)
        self.assert_unchanged()

    def test_authenticated_api_and_origin_validation_cover_preview_and_confirm(self):
        password = "test-only-cycle-recovery-password"
        with patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": password}, clear=True):
            app = create_app(self.engine, start_engine=False)
        with TestClient(app) as client:
            urls = ["/api/accounts/test/cycle/" + suffix for suffix in ("recovery-preview", "recovery-confirm")]
            for url in urls:
                self.assertEqual(client.post(url, json={"token": "a" * 32}).status_code, 401)
            client.headers["origin"] = "http://testserver"
            self.assertEqual(client.post("/api/login", json={"password": password}).status_code, 200)
            for url in urls:
                self.assertEqual(client.post(url, json={"token": "a" * 32}, headers={"origin": "https://other.invalid"}).status_code, 403)
            result = client.post(urls[0])
            self.assertEqual(result.status_code, 200, result.text)
            token = result.json()["token"]
            for body in ({}, {"token": None}, {"token": 12}, {"token": token, "actual": {"LONG": "999"}}):
                self.assertEqual(client.post(urls[1], json=body).status_code, 422)
            self.assert_unchanged()
            result = client.post(urls[1], json={"token": token})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertFalse(self.f.store.account("test")["enabled"])
