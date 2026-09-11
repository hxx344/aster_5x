"""Per-account risk settings across API, restart, planning and paper execution."""
import copy
from dataclasses import replace
from fractions import Fraction
import os
import re
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from trading.engine import DEFAULT_POLICY, Engine, validate_account
from trading.execution import Executor
from trading.models import Book, Plan, Position, TradingError, dec, next_leverage, plan_pair
from trading.server import create_app
from trading.store import Store, dumps
from .helpers import Fixture, account


SYMBOL = "XAUUSD1"
PASSWORD = "test-only-configurable-policy-password"


class ConfigurablePolicyAPITests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.account["enabled"] = False
        self.f.store.save_account(self.f.account)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        env = patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": PASSWORD}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(create_app(self.engine, start_engine=False))
        self.addCleanup(self.client.close)
        self.client.headers["origin"] = "http://testserver"
        response = self.client.post("/api/login", json={"password": PASSWORD})
        self.assertEqual(response.status_code, 200, response.text)

    def update(self, changes, account_id="test"):
        return self.client.patch("/api/accounts/" + account_id, json=changes)

    def test_new_account_defaults_are_fifty_percent_and_four(self):
        response = self.client.post("/api/accounts", json={"id": "second", "name": "第二个模拟账户",
                                                            "mode": "paper", "env_prefix": "ASTER_SECOND"})
        self.assertEqual(response.status_code, 200, response.text)
        saved = self.f.store.account("second")
        self.assertFalse(saved["enabled"])
        self.assertEqual(dec(saved["policy"]["margin_limit"]), dec(".5"))
        self.assertEqual(saved["policy"]["min_open_leverage"], 4)

    def test_legacy_two_field_patch_and_each_new_field_can_be_updated_independently(self):
        before = copy.deepcopy(self.f.broker.state)
        for changes in ({"threshold": "25000", "order_notional": "700"},
                        {"margin_limit": "0.7"}, {"min_open_leverage": 7}):
            response = self.update(changes)
            self.assertEqual(response.status_code, 200, response.text)
        policy = self.f.store.account("test")["policy"]
        self.assertEqual((policy["threshold"], policy["order_notional"], policy["margin_limit"], policy["min_open_leverage"]),
                         ("25000", "700", "0.7", 7))
        self.assertEqual(self.f.broker.state, before)

    def test_margin_accepts_thirty_seventy_and_one_hundred_percent(self):
        for value in ("0.3", "0.7", "1"):
            with self.subTest(value=value):
                response = self.update({"margin_limit": value})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(dec(self.f.store.account("test")["policy"]["margin_limit"]), dec(value))

    def test_minimum_leverage_accepts_both_bounds_and_nonstandard_tiers(self):
        for value in (1, 2, 7, 10, 125):
            with self.subTest(value=value):
                response = self.update({"min_open_leverage": value})
                self.assertEqual(response.status_code, 200, response.text)
                saved = self.f.store.account("test")["policy"]["min_open_leverage"]
                self.assertIs(type(saved), int)
                self.assertEqual(saved, value)

    def test_margin_scientific_notation_can_be_resaved_as_expanded_decimal(self):
        for value in ("1e-50", "0." + "0" * 49 + "1"):
            response = self.update({"margin_limit": value, "min_open_leverage": 7})
            self.assertEqual(response.status_code, 200, response.text)
            saved = self.f.store.account("test")["policy"]
            self.assertEqual(dec(saved["margin_limit"]), dec("1e-50"))
            self.assertEqual(saved["min_open_leverage"], 7)

    def test_invalid_updates_are_atomic_and_do_not_persist_any_other_supplied_field(self):
        invalid = [{"min_open_leverage": value} for value in (None, True, False, 0, 126, 7.0, 1.5, "7", [], {})]
        invalid += [{"margin_limit": value} for value in (None, True, False, 0.7, "0", "-0.1", "1.00001", "NaN", "Infinity", "")]
        invalid += [{"unknown_setting": "value"}, {"symbols": [SYMBOL]}, {"hedge_mode": False}]
        original = self.f.store.account("test")
        for changes in invalid:
            with self.subTest(changes=changes):
                response = self.update({"threshold": "99999", **changes})
                self.assertIn(response.status_code, (409, 422), response.text)
                self.assertEqual(self.f.store.account("test"), original)

    def test_running_or_pending_account_cannot_change_either_new_setting(self):
        for running, pending in ((True, False), (False, True)):
            saved = self.f.store.account("test")
            saved["enabled"] = running
            self.f.store.save_account(saved)
            if pending:
                self.f.store.save_intent({"id": "pending-config-lock", "account_id": "test", "kind": "leverage",
                                          "symbol": SYMBOL, "previous": 4, "target": 5,
                                          "status": "pending", "created_at": time.time()})
            before = self.f.store.account("test")
            with self.subTest(running=running, pending=pending):
                response = self.update({"margin_limit": "0.7", "min_open_leverage": 7})
                self.assertEqual(response.status_code, 409, response.text)
                self.assertEqual(self.f.store.account("test"), before)

    def test_account_settings_are_isolated_and_survive_restart(self):
        second = account("second")
        second["enabled"] = False
        self.f.store.save_account(second)
        for account_id, margin, minimum in (("test", "0.3", 2), ("second", "0.7", 7)):
            response = self.update({"margin_limit": margin, "min_open_leverage": minimum}, account_id)
            self.assertEqual(response.status_code, 200, response.text)
        restarted = Store(self.f.store.path)
        state = Engine(restarted, market=self.f.market).state()
        policies = {saved["id"]: saved["policy"] for saved in state["accounts"]}
        self.assertEqual((policies["test"]["margin_limit"], policies["test"]["min_open_leverage"]), ("0.3", 2))
        self.assertEqual((policies["second"]["margin_limit"], policies["second"]["min_open_leverage"]), ("0.7", 7))

    def test_old_accounts_gain_default_minimum_without_overwriting_custom_margin(self):
        for name, margin in (("old_low", "0.30"), ("old_high", "0.70")):
            legacy = account(name)
            legacy["enabled"] = False
            legacy["policy"].pop("min_open_leverage", None)
            legacy["policy"]["margin_limit"] = margin
            with self.f.store.connect() as db:
                db.execute("INSERT INTO accounts VALUES (?,?)", (name, dumps(legacy)))
        restarted = Store(self.f.store.path)
        for name, margin in (("old_low", "0.30"), ("old_high", "0.70")):
            with self.subTest(account=name):
                saved = restarted.account(name)
                self.assertEqual(saved["policy"]["margin_limit"], margin)
                self.assertEqual(saved["policy"]["min_open_leverage"], 4)
                self.assertEqual(validate_account(saved), saved)
        self.assertTrue(all(saved["policy"]["min_open_leverage"] == 4 for saved in restarted.accounts()))


class ConfigurablePolicyExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.executor = Executor(self.f.store, self.f.broker, self.f.market)

    def configure_fixture(self, *, minimum=4, margin="0.5", leverage=4, qty="0", enabled=True):
        self.f.account.update(enabled=enabled)
        self.f.account["policy"].update(min_open_leverage=minimum, margin_limit=margin)
        self.f.store.save_account(self.f.account)
        self.f.broker.state["leverages"][SYMBOL] = leverage
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"][SYMBOL + ":" + side] = {"qty": qty, "entry": "4412" if dec(qty) else "0"}
        self.f.broker.save()

    def publish(self, capacities):
        with patch.object(self.f.market, "capacities", return_value=capacities):
            self.engine.poll_market(SYMBOL)

    def test_minimum_two_allows_real_paper_market_fills_at_two(self):
        self.configure_fixture(minimum=2, leverage=2)
        snapshot, book = self.f.broker.snapshot([SYMBOL]), self.f.market.book(SYMBOL)
        plan = plan_pair(snapshot, book, self.f.market.rules[SYMBOL], {2: dec(500000)}, self.f.account["policy"])
        self.assertGreater(plan.qty, 0)
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.executor.open_pair(self.f.account, snapshot, SYMBOL, plan, book)
        submit.assert_called_once()
        self.assertTrue(all(order["type"] == "MARKET" for order in submit.call_args.args[0]))
        long, short = self.f.broker.snapshot([SYMBOL]).pair(SYMBOL)
        self.assertEqual((long.qty, short.qty, long.leverage), (plan.qty, plan.qty, 2))

    def test_minimum_ten_rejects_plans_and_supplied_entry_at_four_or_five(self):
        for leverage in (4, 5):
            self.configure_fixture(minimum=10, leverage=leverage)
            snapshot, book = self.f.broker.snapshot([SYMBOL]), self.f.market.book(SYMBOL)
            before = copy.deepcopy(self.f.broker.state)
            with self.subTest(leverage=leverage):
                plan = plan_pair(snapshot, book, self.f.market.rules[SYMBOL], {leverage: dec(500000)}, self.f.account["policy"])
                self.assertEqual(plan.qty, 0)
                self.assertIn("10x", plan.reason)
                with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not submit below account minimum")):
                    with self.assertRaisesRegex(TradingError, "10x"):
                        self.executor.open_pair(self.f.account, snapshot, SYMBOL, Plan(dec(".005")), book)
                self.assertIsNone(self.f.store.intent("test"))
                self.assertEqual(self.f.broker.state, before)

    def test_nonstandard_minimum_seven_is_selected_confirmed_and_then_used_for_entry(self):
        self.configure_fixture(minimum=7, leverage=2)
        capacities = {tier: dec(500000) for tier in (2, 4, 5, 7, 10, 20)}
        self.publish(capacities)
        with patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as change, \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            for index, target in enumerate((4, 5, 7), start=1):
                self.publish(capacities)
                self.engine.tick_account("test")
                self.assertEqual(change.call_count, index)
                self.assertEqual(change.call_args.args, (SYMBOL, target))
                submit.assert_not_called()
                self.assertEqual(self.f.store.intent("test")["target"], target)
                self.publish(capacities)
                self.engine.tick_account("test")
                self.assertIsNone(self.f.store.intent("test"))
            self.publish(capacities)
            self.engine.tick_account("test")
        submit.assert_called_once()
        long, short = self.f.broker.snapshot([SYMBOL]).pair(SYMBOL)
        self.assertGreater(long.qty, 0)
        self.assertEqual((long.qty, long.leverage, short.leverage), (short.qty, 7, 7))

    def test_higher_selection_keeps_base_tiers_below_configured_opening_minimum(self):
        self.configure_fixture(minimum=7, leverage=2)
        snapshot = self.f.broker.snapshot([SYMBOL])
        for capacities, expected in (({4: dec(500000), 5: dec(500000), 7: dec(500000)}, 4),
                                     ({4: dec(10000), 5: dec(500000), 7: dec(0), 10: dec(500000)}, 5),
                                     ({4: dec(0), 5: dec(0), 7: dec(0), 10: dec(500000)}, 10),
                                     ({4: dec(0), 5: dec(0), 7: dec(0), 10: dec(0)}, None)):
            with self.subTest(capacities=capacities):
                self.assertEqual(next_leverage(snapshot, SYMBOL, capacities, threshold=10000, min_open_leverage=7), expected)

    def test_lowering_minimum_does_not_reduce_existing_actual_leverage(self):
        self.configure_fixture(minimum=10, leverage=10, enabled=False)
        with patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("configuration must not lower leverage")):
            self.engine.configure("test", {"min_open_leverage": 2})
            self.engine.enable("test", True)
            self.publish({tier: dec(500000) for tier in (2, 4, 5, 10)})
            self.engine.tick_account("test")
        long, short = self.f.broker.snapshot([SYMBOL]).pair(SYMBOL)
        self.assertEqual((long.leverage, short.leverage), (10, 10))
        self.assertGreater(long.qty, 0)

    def legacy_pending(self, *, one_leg):
        self.configure_fixture(minimum=10, leverage=2, qty="1", enabled=False)
        orders = [{"symbol": SYMBOL, "positionSide": side, "side": "BUY" if side == "LONG" else "SELL",
                   "type": "MARKET", "quantity": "0.005", "newClientOrderId": "old-" + side,
                   "newOrderRespType": "RESULT"} for side in ("LONG", "SHORT")]
        receipts = self.f.broker.submit(orders[:1] if one_leg else orders)
        if one_leg:
            receipts.append({**orders[1], "clientOrderId": orders[1]["newClientOrderId"], "status": "REJECTED",
                             "executedQty": "0", "avgPrice": "0"})
        self.f.store.save_intent({"id": "old-batch", "account_id": "test", "kind": "pair", "symbol": SYMBOL,
                                  "leverage": 2, "status": "pending", "created_at": time.time(), "orders": orders,
                                  "baseline": {"LONG": "1", "SHORT": "1"}, "repairs": [], "repair_attempts": 0,
                                  "receipts": {row["clientOrderId"]: row for row in receipts}})

    def test_old_pending_below_new_minimum_still_reconciles_without_reopening(self):
        self.legacy_pending(one_leg=False)
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not duplicate old entries")), \
             patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("must reconcile first")):
            self.engine.tick_account("test")
        self.assertIsNone(self.f.store.intent("test"))
        long, short = self.f.broker.snapshot([SYMBOL]).pair(SYMBOL)
        self.assertEqual((long.qty, short.qty, long.leverage), (dec("1.005"), dec("1.005"), 2))
        self.assertFalse(self.f.store.account("test")["enabled"])

    def test_old_pending_single_leg_below_new_minimum_still_repairs_only_new_excess(self):
        self.legacy_pending(one_leg=True)
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit, \
             patch.object(self.f.broker, "set_leverage", side_effect=AssertionError("must not change leverage during repair")):
            self.engine.tick_account("test")
        submit.assert_called_once()
        repair, = submit.call_args.args[0]
        self.assertEqual((repair["type"], repair["positionSide"], repair["side"], dec(repair["quantity"])),
                         ("MARKET", "LONG", "SELL", dec(".005")))
        self.assertIsNone(self.f.store.intent("test"))
        long, short = self.f.broker.snapshot([SYMBOL]).pair(SYMBOL)
        self.assertEqual((long.qty, short.qty, long.leverage), (1, 1, 2))


class ConfigurableMarginPlanningTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        base = self.f.broker.snapshot([SYMBOL])
        self.snapshot = replace(base, equity=dec(1000), wallet=dec(1000), available=dec(1000), maintenance=dec(0),
                                positions=[Position(SYMBOL, side, dec(0), dec(0), dec(100), 4) for side in ("LONG", "SHORT")],
                                fees={SYMBOL: dec(0)}, unrealized=dec(0))
        self.book = Book(dec(100), dec(100), dec(1000), dec(1000), dec(100), time.time())
        self.policy = {**DEFAULT_POLICY, "symbols": [SYMBOL], "order_notional": "100000", "min_open_leverage": 4}

    def test_exact_thirty_seventy_and_one_hundred_percent_planning_boundaries(self):
        for margin, expected_qty in (("0.3", 6), ("0.7", 14), ("1", 20)):
            policy = {**self.policy, "margin_limit": margin}
            with self.subTest(margin=margin):
                plan = plan_pair(self.snapshot, self.book, self.f.market.rules[SYMBOL], {4: dec(500000)}, policy)
                self.assertEqual(plan.qty, expected_qty)
                self.assertEqual(plan.projected_ratio, dec(margin))
                next_qty = Fraction(plan.qty + self.f.market.rules[SYMBOL].step)
                self.assertGreater(2 * next_qty * 100 / 4, Fraction(dec(margin)) * 1000)

    def test_fees_and_spread_still_limit_the_last_quantity_step_for_each_margin_setting(self):
        snapshot = replace(self.snapshot, fees={SYMBOL: dec(".0004")})
        book = replace(self.book, ask=dec("100.01"), mark=dec("100.005"))
        for margin in ("0.3", "0.7", "1"):
            with self.subTest(margin=margin):
                plan = plan_pair(snapshot, book, self.f.market.rules[SYMBOL], {4: dec(500000)},
                                 {**self.policy, "margin_limit": margin})
                quantity, limit = Fraction(plan.qty), Fraction(dec(margin))
                cost_per_qty = Fraction(dec(".01")) + Fraction(dec("200.01")) * Fraction(dec(".0004"))
                used_per_qty = Fraction(dec("200.02")) / 4
                self.assertGreater(quantity, 0)
                self.assertLessEqual(quantity * used_per_qty, limit * (1000 - quantity * cost_per_qty))
                next_qty = quantity + Fraction(self.f.market.rules[SYMBOL].step)
                self.assertGreater(next_qty * used_per_qty, limit * (1000 - next_qty * cost_per_qty))

    def test_post_fill_pause_uses_configured_limit_and_allows_equality(self):
        for margin in ("0.3", "0.7", "1"):
            for exceeds in (False, True):
                current = account()
                current["policy"].update(margin_limit=margin, min_open_leverage=4)
                self.f.store.save_account(current)
                ratio = dec(margin) + (dec(".000001") if exceeds else dec(0))
                qty = ratio * 20
                snapshot = replace(self.snapshot, timestamp=time.time(), available=1000 * (1 - ratio),
                                   positions=[Position(SYMBOL, side, qty, dec(100), dec(100), 4) for side in ("LONG", "SHORT")])
                self.f.store.put("post_fill_check:test", True)
                with self.subTest(margin=margin, exceeds=exceeds):
                    self.assertEqual(snapshot.ratio, ratio)
                    self.assertEqual(self.engine.check_post_fill_occupancy(current, snapshot), exceeds)
                    self.assertEqual(self.f.store.account("test")["enabled"], not exceeds)
                    self.assertIsNone(self.f.store.get("post_fill_check:test"))


class ConfigurablePolicyMarketTests(unittest.TestCase):
    def test_single_poll_requests_all_relevant_configured_and_actual_tiers_but_alerts_only_original_four(self):
        f = Fixture()
        self.addCleanup(f.close)
        first = f.store.account("test")
        first["policy"]["min_open_leverage"] = 7
        f.store.save_account(first)
        second = account("second")
        second["policy"]["min_open_leverage"] = 2
        f.store.save_account(second)
        other_symbol = account("other_symbol")
        other_symbol["policy"].update(symbols=["CLUSD1"], min_open_leverage=9)
        f.store.save_account(other_symbol)
        engine = Engine(f.store, market=f.market)
        engine.views["test"] = {"snapshot": {"positions": [{"symbol": SYMBOL, "leverage": 13},
                                                             {"symbol": "CLUSD1", "leverage": 9}]}}
        engine.views["second"] = {"snapshot": {"positions": [{"symbol": SYMBOL, "leverage": 2}]}}
        webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/test-configurable-policy"
        with patch.dict(os.environ, {"FEISHU_WEBHOOK_URL": webhook}, clear=True), \
             patch("trading.engine.monitor.send_feishu") as sender, \
             patch.object(f.market, "capacities", side_effect=lambda symbol, tiers: {tier: dec(20000) for tier in tiers}) as capacities, \
             patch.object(f.market, "book", wraps=f.market.book) as book:
            engine.poll_market(SYMBOL)
            capacities.assert_called_once()
            book.assert_not_called()
            queried_symbol, queried_tiers = capacities.call_args.args
            self.assertEqual(queried_symbol, SYMBOL)
            self.assertEqual(set(queried_tiers), {2, 4, 5, 7, 10, 13, 20})
            self.assertEqual(f.store.pending_notifications(), 4)
            engine.notify()
            engine.notify()
        self.assertEqual(sender.call_count, 4)
        announced = {int(re.search(r" · (\d+)x", call.args[1]).group(1)) for call in sender.call_args_list}
        self.assertEqual(announced, {4, 5, 10, 20})


if __name__ == "__main__":
    unittest.main()
