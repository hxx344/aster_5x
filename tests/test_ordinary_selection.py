"""Market selection through authenticated settings, dispatch and paper execution."""
from copy import deepcopy
import os
import time
from unittest import TestCase
from unittest.mock import patch

from fastapi.testclient import TestClient

from tests.helpers import Fixture, account
from trading.cycle_guard import ordinary_add_blocks, ordinary_add_symbols
from trading.engine import Engine, validate_account
from trading.execution import Executor
from trading.models import SYMBOLS, TradingError, dec, plan_pair
from trading.server import create_app
from trading.store import Store, dumps


class OrdinarySelectionTests(TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.owner = self.f.store.account("test")
        self.owner["enabled"] = False
        self.owner["policy"]["symbols"] = list(SYMBOLS)
        self.f.store.save_account(self.owner)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.executor = Executor(self.f.store, self.f.broker, self.f.market)

    def select(self, symbol):
        self.engine.configure("test", {"ordinary_symbol": symbol})
        return self.f.store.account("test")

    def publish(self, enabled=SYMBOLS, tiers=(5,)):
        for symbol in SYMBOLS:
            self.engine.markets[symbol] = {
                "status": "ok", "checked_at": time.time(),
                "capacities": {str(tier): "500000" if symbol in enabled and tier in tiers else "0"
                               for tier in (5, 10, 20)},
            }

    def plan(self):
        snapshot = self.f.broker.snapshot(list(SYMBOLS))
        book = self.f.market.book("XAUUSD1")
        plan = plan_pair(snapshot, book, self.f.market.rules["XAUUSD1"],
                         {5: dec(500000)}, self.owner["policy"])
        self.assertGreater(plan.qty, 0)
        return snapshot, plan, book

    def client(self):
        env = patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": "selection-test-only"}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        client = TestClient(create_app(self.engine, start_engine=False))
        self.addCleanup(client.close)
        client.headers["origin"] = "http://testserver"
        self.assertEqual(client.post("/api/login", json={"password": "selection-test-only"}).status_code, 200)
        return client

    def test_api_selects_each_market_and_restores_all_without_trading(self):
        client = self.client()
        before = deepcopy(self.f.broker.state)
        for selected in (*SYMBOLS, "all"):
            response = client.patch("/api/accounts/test", json={"ordinary_symbol": selected})
            self.assertEqual(response.status_code, 200, response.text)
            saved = self.f.store.account("test")
            self.assertEqual(saved["policy"]["ordinary_symbol"], selected)
            self.assertEqual(ordinary_add_symbols(saved), list(SYMBOLS) if selected == "all" else [selected])
            self.assertFalse(saved["enabled"])
        self.assertEqual(self.f.broker.state, before)

    def test_invalid_api_updates_are_atomic(self):
        client = self.client()
        before = self.f.store.account("test")
        for value in (None, "", "BTCUSDT", "xauusd1", True, 1, [], {}, ["CLUSD1"]):
            with self.subTest(value=value):
                response = client.patch("/api/accounts/test", json={"ordinary_symbol": value, "threshold": "99"})
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.f.store.account("test"), before)

    def test_running_or_pending_account_rejects_selector_update(self):
        client = self.client()
        for running in (True, False):
            saved = self.f.store.account("test")
            saved["enabled"] = running
            self.f.store.save_account(saved)
            if not running:
                self.f.store.save_intent({"id": "pending", "account_id": "test", "status": "pending"})
            response = client.patch("/api/accounts/test", json={"ordinary_symbol": "CLUSD1"})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(self.f.store.account("test")["policy"]["ordinary_symbol"], "all")

    def test_restart_and_state_preserve_account_isolation_and_replace_stale_blocks(self):
        self.select("CLUSD1")
        self.f.store.save_account(account("second"))
        restarted = Engine(Store(self.f.store.path), market=self.f.market)
        restarted.view("test", ordinary_add_blocks={"CLUSD1": "stale"})
        state = {row["id"]: row for row in restarted.state()["accounts"]}
        self.assertEqual(state["test"]["policy"]["ordinary_symbol"], "CLUSD1")
        self.assertEqual(set(state["test"]["ordinary_add_blocks"]), set(SYMBOLS) - {"CLUSD1"})
        self.assertEqual(state["second"]["ordinary_add_blocks"], {})

    def test_legacy_default_and_invalid_saved_selection(self):
        legacy = deepcopy(self.owner)
        legacy["policy"].pop("ordinary_symbol")
        with self.f.store.connect() as db:
            db.execute("UPDATE accounts SET data=? WHERE id='test'", (dumps(legacy),))
        saved = Store(self.f.store.path).account("test")
        self.assertEqual(saved["policy"]["ordinary_symbol"], "all")
        self.assertEqual(ordinary_add_symbols(validate_account(legacy)), list(SYMBOLS))
        for value in (None, "", "BTCUSDT", [], True):
            invalid = deepcopy(saved)
            invalid["policy"]["ordinary_symbol"] = value
            with self.subTest(value=value), self.assertRaises(TradingError):
                validate_account(invalid)
            with self.assertRaises(TradingError):
                ordinary_add_blocks(invalid)
        saved["policy"].update(symbols=["XAUUSD1"], ordinary_symbol="CLUSD1")
        with self.assertRaises(TradingError):
            validate_account(saved)

    def test_engine_fills_only_selected_market_when_all_have_capacity(self):
        for selected in SYMBOLS:
            with self.subTest(selected=selected):
                self.engine.enable("test", False)
                self.select(selected)
                self.engine.enable("test", True)
                self.publish()
                before = set(self.f.broker.state["orders"])
                self.engine.tick_account("test")
                orders = [order for key, order in self.f.broker.state["orders"].items() if key not in before]
                self.assertEqual(len(orders), 2, self.engine.views)
                self.assertEqual({order["symbol"] for order in orders}, {selected})
                long, short = self.f.broker.snapshot(list(SYMBOLS)).pair(selected)
                self.assertGreater(long.qty, 0)
                self.assertEqual(long.qty, short.qty)

    def test_no_capacity_does_not_fall_back_or_obey_stale_priority_elsewhere(self):
        self.select("CLUSD1")
        self.engine.enable("test", True)
        self.publish(enabled=("XAUUSD1", "SPCXUSD1"), tiers=(5, 10, 20))
        self.engine.active_priority_accounts.add("test")
        self.engine.active_priority_signals["test"] = {"XAUUSD1": time.time()}
        with patch.object(self.f.broker, "submit") as submit, patch.object(self.f.broker, "set_leverage") as leverage:
            self.engine.tick_account("test")
        submit.assert_not_called()
        leverage.assert_not_called()
        self.assertTrue(self.f.store.account("test")["enabled"])

    def test_capacity_wakes_follow_each_accounts_selection(self):
        self.select("CLUSD1")
        self.engine.enable("test", True)
        other = account("second")
        other["policy"]["symbols"] = list(SYMBOLS)
        self.f.store.save_account(other)
        for symbol in SYMBOLS:
            self.engine.wake_capacity_accounts(symbol, {10: dec(500000)}, time.time(), self.f.store.accounts())
        self.assertEqual(set(self.engine.priority_accounts["test"]), {"CLUSD1"})
        self.assertEqual(set(self.engine.priority_accounts["second"]), set(SYMBOLS))

    def test_executor_rechecks_saved_selection_before_creating_pair_intent(self):
        snapshot, plan, book = self.plan()
        for stale in (False, True):
            self.select("all")
            owner = self.f.store.account("test")
            self.select("CLUSD1")
            if not stale:
                owner = self.f.store.account("test")
            with self.subTest(stale=stale), patch.object(self.f.broker, "submit") as submit, \
                 patch.object(self.f.store, "save_intent") as save_intent:
                with self.assertRaisesRegex(TradingError, "仅限 CLUSD1"):
                    self.executor.open_pair(owner, snapshot, "XAUUSD1", plan, book)
            submit.assert_not_called()
            save_intent.assert_not_called()

    def test_selection_change_during_quote_check_prevents_submission(self):
        snapshot, plan, book = self.plan()
        with patch.object(book, "require_fresh", side_effect=lambda: self.select("CLUSD1")), \
             patch.object(self.f.broker, "submit") as submit:
            with self.assertRaisesRegex(TradingError, "仅限 CLUSD1"):
                self.executor.open_pair(self.owner, snapshot, "XAUUSD1", plan, book)
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_ordinary_upgrade_rechecks_selection_and_migration_upgrade_is_independent(self):
        self.select("CLUSD1")
        with patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as leverage:
            with self.assertRaisesRegex(TradingError, "仅限 CLUSD1"):
                self.executor.leverage(self.owner, "XAUUSD1", 5, 10)
            leverage.assert_not_called()
            self.assertIsNone(self.f.store.intent("test"))
            self.executor.leverage(self.f.store.account("test"), "XAUUSD1", 5, 10,
                                   purpose="migration", symbols=list(SYMBOLS))
            leverage.assert_called_once()
        self.assertEqual(self.f.broker.snapshot(list(SYMBOLS)).pair("XAUUSD1")[0].leverage, 10)

    def test_cycle_keeps_ownership_and_can_open_selected_market(self):
        self.select("CLUSD1")
        self.engine.configure("test", {"cycle": {"enabled": True, "symbol": "CLUSD1", "leverage": 2,
                                                   "spread_limit_bp": "5", "spread_notional": "1000", "max_notional": "1000"}})
        self.f.broker.set_cycle_leverage("CLUSD1", 2)
        owner = self.f.store.account("test")
        self.assertEqual(ordinary_add_symbols(owner), [])
        self.assertIn("成交量循环", ordinary_add_blocks(owner)["CLUSD1"])
        self.engine.enable("test", True)
        self.publish()
        self.engine.tick_account("test")
        self.assertEqual(self.f.store.get("cycle:test")["phase"], "holding")
        self.assertEqual({order["symbol"] for order in self.f.broker.state["orders"].values()}, {"CLUSD1"})

    def test_existing_partial_pair_recovers_after_selection_changes(self):
        snapshot, plan, book = self.plan()
        original_submit = self.f.broker.submit

        def partial(orders):
            return [original_submit(orders[:1])[0], {"code": -2019}]

        with patch.object(self.f.broker, "submit", side_effect=partial), \
             patch.object(self.executor, "reconcile", return_value="interrupted"):
            self.executor.open_pair(self.owner, snapshot, "XAUUSD1", plan, book)
        saved = self.f.store.account("test")
        saved["policy"]["ordinary_symbol"] = "CLUSD1"
        self.f.store.save_account(saved)
        with patch.object(self.f.broker, "submit", wraps=original_submit) as submit:
            self.executor.reconcile(saved)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual([(order["symbol"], order["side"]) for order in submit.call_args.args[0]], [("XAUUSD1", "SELL")])
        self.assertEqual(tuple(p.qty for p in self.f.broker.snapshot(list(SYMBOLS)).pair("XAUUSD1")), (0, 0))
        self.assertIsNone(self.f.store.intent("test"))
