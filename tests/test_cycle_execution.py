import copy
import time
import unittest
from unittest.mock import Mock, patch

from trading.cycle import CyclePlan, DEFAULT_CYCLE
from trading.cycle_execution import CycleExecutor
from trading.exchange import AmbiguousOrder, ExchangeError, LiveBroker, RequestNotSent
from trading.models import TradingError, dec
from trading.paper import PaperBroker
from trading.store import Store
from .helpers import Fixture


class CycleExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.account["cycle"] = {**DEFAULT_CYCLE, "enabled": True}
        self.f.store.save_account(self.f.account)
        self.progress = {"run_id": "cycle-test", "phase": "waiting_open", "quantities": {"LONG": "0", "SHORT": "0"},
                         "opened_at": None, "completed_cycles": 0, "config": copy.deepcopy(self.f.account["cycle"])}
        self.f.store.put("cycle:test", self.progress)
        self.f.broker.set_cycle_leverage("XAUUSD1", 2)
        self.executor = CycleExecutor(self.f.store, self.f.broker, self.f.market)

    def progress_now(self):
        return self.f.store.get("cycle:test")

    def plan(self, phase):
        book = self.f.market.book("XAUUSD1")
        return CyclePlan(phase, "XAUUSD1", dec(2), 2, dec(2) * book.ask, dec(2) * book.bid, dec("0.02"))

    def open(self):
        return self.executor.start(self.f.account, self.f.broker.cycle_snapshot(["XAUUSD1"]), self.plan("open"), self.progress_now())

    def close_cycle(self):
        progress = self.progress_now()
        progress.update(opened_at=time.time() - 61, phase="waiting_close")
        self.f.store.put("cycle:test", progress)
        return self.executor.start(self.f.account, self.f.broker.cycle_snapshot(["XAUUSD1"]), self.plan("close"), progress)

    def quantities(self):
        return tuple(p.qty for p in self.f.broker.cycle_snapshot(["XAUUSD1"]).pair("XAUUSD1"))

    def test_full_cycle_records_holding_only_after_confirmed_fill_and_flat_closure(self):
        self.open()
        progress = self.progress_now()
        self.assertEqual(progress["phase"], "holding")
        self.assertEqual(progress["quantities"], {"LONG": "2", "SHORT": "2"})
        self.assertGreater(progress["opened_at"], time.time() - 5)
        self.assertEqual(self.quantities(), (2, 2))
        self.assertIsNone(self.f.store.get("campaign:test"))
        self.close_cycle()
        self.assertEqual(self.quantities(), (0, 0))
        self.assertEqual(self.progress_now()["phase"], "waiting_open")
        self.assertEqual(self.progress_now()["completed_cycles"], 1)
        self.assertIsNone(self.progress_now()["opened_at"])
        self.assertIsNone(self.f.store.intent("test"))

    def test_timeout_after_acceptance_queries_without_resending_open(self):
        original = self.f.broker.submit
        def accepted(orders):
            original(orders)
            raise AmbiguousOrder("timeout after acceptance")
        with patch.object(self.f.broker, "submit", side_effect=accepted) as submit:
            self.open()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.progress_now()["phase"], "holding")

    def test_unknown_live_style_receipts_pause_without_any_resubmission(self):
        with patch.object(self.f.broker, "submit", side_effect=AmbiguousOrder("unknown write")) as submit, \
             patch.object(self.f.broker, "query", side_effect=ExchangeError("not visible", code=-2013)):
            self.open()
            pending = self.f.store.intent("test")
            pending["created_at"] -= 130
            self.f.store.save_intent(pending)
            self.executor.reconcile(self.f.account)
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.f.store.intent("test")["status"], "attention")
        self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertIsNone(self.progress_now()["opened_at"])

    def test_one_leg_open_rejection_flattens_only_filled_side(self):
        original = self.f.broker.submit
        def partial(orders):
            return [original(orders[:1])[0], {"code": -2019}] if len(orders) == 2 else original(orders)
        with patch.object(self.f.broker, "submit", side_effect=partial) as submit:
            self.open()
        self.assertEqual(submit.call_count, 2)
        repair = submit.call_args_list[1].args[0][0]
        self.assertEqual((repair["positionSide"], repair["side"], repair["quantity"]), ("LONG", "SELL", "2"))
        self.assertEqual(self.quantities(), (0, 0))
        self.assertEqual(self.progress_now()["completed_cycles"], 0)

    def test_both_legs_partial_open_are_fully_flattened(self):
        original_book = self.f.market.book
        def limited_book(symbol):
            book = original_book(symbol)
            book.bid_qty = book.ask_qty = dec(1)
            return book
        with patch.object(self.f.market, "book", side_effect=limited_book), patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.open()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual([(o["positionSide"], o["side"], o["quantity"]) for o in submit.call_args_list[1].args[0]],
                         [("LONG", "SELL", "1"), ("SHORT", "BUY", "1")])
        self.assertEqual(self.quantities(), (0, 0))
        self.assertEqual(self.progress_now()["phase"], "waiting_open")

    def test_partial_close_repairs_only_remaining_short(self):
        self.open()
        original = self.f.broker.submit
        def partial(orders):
            return [original(orders[:1])[0], {"code": -2019}] if len(orders) == 2 else original(orders)
        with patch.object(self.f.broker, "submit", side_effect=partial) as submit:
            self.close_cycle()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual([(o["positionSide"], o["side"], o["quantity"]) for o in submit.call_args_list[1].args[0]], [("SHORT", "BUY", "2")])
        self.assertEqual(self.progress_now()["completed_cycles"], 1)
        self.assertEqual(self.quantities(), (0, 0))

    def test_external_change_blocks_repair_and_preserves_position(self):
        original = self.f.broker.submit
        def changed(orders):
            first = original(orders[:1])[0]
            self.f.broker.state["positions"]["XAUUSD1:LONG"]["qty"] = "3"
            self.f.broker.save()
            return [first, {"code": -2019}]
        with patch.object(self.f.broker, "submit", side_effect=changed) as submit:
            self.open()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.quantities(), (3, 0))
        self.assertEqual(self.f.store.intent("test")["status"], "attention")

    def test_existing_positions_are_frozen_and_restored_after_cycle(self):
        for side in ("LONG", "SHORT"):
            self.f.broker.state["positions"]["XAUUSD1:" + side] = {"qty": "1", "entry": "4400"}
        self.f.broker.save()
        snapshot = self.f.broker.cycle_snapshot(["XAUUSD1"])
        snapshot.open_orders = None
        self.executor.start(self.f.account, snapshot, self.plan("open"), self.progress_now())
        self.assertEqual(self.quantities(), (3, 3))
        self.assertEqual(self.progress_now()["baseline"], {"LONG": "1", "SHORT": "1"})
        self.assertEqual(self.progress_now()["quantities"], {"LONG": "2", "SHORT": "2"})
        self.close_cycle()
        self.assertEqual(self.quantities(), (1, 1))

    def test_unqueried_external_order_inventory_does_not_block_taker_cycle(self):
        snapshot = self.f.broker.cycle_snapshot(["XAUUSD1"])
        snapshot.open_orders = None
        self.executor.start(self.f.account, snapshot, self.plan("open"), self.progress_now())
        self.assertEqual(self.quantities(), (2, 2))

    def test_hold_minimum_is_enforced_at_execution_boundary(self):
        self.open()
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not send")), self.assertRaisesRegex(TradingError, "持仓时间"):
            self.executor.start(self.f.account, self.f.broker.cycle_snapshot(["XAUUSD1"]), self.plan("close"), self.progress_now())

    def test_restart_after_submission_recovers_receipts_and_counts_close_once(self):
        self.open()
        with patch.object(self.executor, "reconcile", return_value="simulated crash"):
            self.close_cycle()
        restored_store = Store(self.f.store.path)
        restored = PaperBroker("test", self.f.market, restored_store)
        executor = CycleExecutor(restored_store, restored, self.f.market)
        pending = copy.deepcopy(restored_store.intent("test"))
        with patch.object(restored, "submit", side_effect=AssertionError("must not resend")):
            executor.reconcile(self.f.account)
            executor.reconcile(self.f.account, pending)
        self.assertEqual(self.progress_now()["completed_cycles"], 1)
        self.assertEqual(self.quantities(), (0, 0))

    def test_definitively_absent_paper_open_returns_flat_without_resend(self):
        with patch.object(self.f.broker, "submit", side_effect=AmbiguousOrder("uncommitted")) as submit:
            self.open()
        self.assertEqual(submit.call_count, 1)
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.progress_now()["phase"], "waiting_open")

    def test_repair_attempts_are_bounded(self):
        original = self.f.broker.submit
        def rejected(orders):
            if orders[0]["side"] == "BUY":
                return [original(orders[:1])[0], {"code": -2019}]
            return [{"code": -2019} for _ in orders]
        with patch.object(self.f.broker, "submit", side_effect=rejected) as submit:
            self.open()
        self.assertEqual(submit.call_count, 4)
        self.assertEqual(self.f.store.intent("test")["repair_attempts"], 3)
        self.assertEqual(self.f.store.intent("test")["status"], "attention")

    def test_local_budget_denial_refunds_repair_attempt_and_can_resume(self):
        original = self.f.broker.submit
        def limited(orders):
            if orders[0]["side"] == "BUY":
                return [original(orders[:1])[0], {"code": -2019}]
            raise RequestNotSent("budget wait", retry_after=10)
        with patch.object(self.f.broker, "submit", side_effect=limited), self.assertRaises(RequestNotSent):
            self.open()
        self.assertEqual(self.f.store.intent("test")["repair_attempts"], 0)
        self.executor.reconcile(self.f.account)
        self.assertEqual(self.quantities(), (0, 0))
        self.assertIsNone(self.f.store.intent("test"))

    def test_cycle_cannot_change_actual_leverage_even_while_flat(self):
        self.f.broker.set_leverage("XAUUSD1", 5)
        with patch.object(self.f.broker, "set_cycle_leverage") as mutate:
            with self.assertRaises(TradingError):
                self.executor.set_leverage(self.f.account, self.f.broker.cycle_snapshot(["XAUUSD1"]), self.progress_now())
        mutate.assert_not_called()
        self.assertEqual(self.f.broker.cycle_snapshot(["XAUUSD1"]).pair("XAUUSD1")[0].leverage, 5)
        self.assertIsNone(self.f.store.intent("test"))

    def test_cycle_cannot_lower_leverage_with_position(self):
        self.open()
        with self.assertRaisesRegex(TradingError, "空仓"):
            self.f.broker.set_cycle_leverage("XAUUSD1", 1)

    def test_submit_rechecks_fresh_account_and_delivers_it_to_callback(self):
        selected = self.f.broker.cycle_snapshot(["XAUUSD1"])
        self.f.broker.state["wallet"] = "24000"
        self.f.broker.save()
        observed = []
        self.executor.start(self.f.account, selected, self.plan("open"), self.progress_now(), before_submit=observed.append)
        self.assertEqual(len(observed), 1)
        self.assertIsNot(observed[0], selected)
        self.assertEqual(observed[0].wallet, 24000)

    def test_fresh_external_position_prevents_submission(self):
        selected = self.f.broker.cycle_snapshot(["XAUUSD1"])
        self.f.broker.state["positions"]["XAUUSD1:LONG"] = {"qty": "1", "entry": "4400"}
        self.f.broker.save()
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not submit")), self.assertRaisesRegex(TradingError, "已变化"):
            self.executor.start(self.f.account, selected, self.plan("open"), self.progress_now())
        self.assertIsNone(self.f.store.intent("test"))

    def test_paused_durable_account_cannot_submit_from_old_enabled_object(self):
        stored = self.f.store.account("test")
        self.f.store.pause_account(stored, "test pause")
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not submit")), self.assertRaisesRegex(TradingError, "未启动"):
            self.open()

    def test_nonfinite_or_bool_holding_start_cannot_authorize_close(self):
        self.open()
        for invalid in (True, float("nan"), float("-inf")):
            progress = self.progress_now()
            progress["opened_at"] = invalid
            with self.subTest(value=invalid), patch.object(self.f.broker, "submit", side_effect=AssertionError("must not submit")), self.assertRaises(TradingError):
                self.executor.start(self.f.account, self.f.broker.cycle_snapshot(["XAUUSD1"]), self.plan("close"), progress)

    def test_nonterminal_receipts_never_start_holding_clock(self):
        original_submit, original_query = self.f.broker.submit, self.f.broker.query
        def pending_submit(orders):
            return [{**row, "status": "NEW"} for row in original_submit(orders)]
        def pending_query(symbol, cid):
            return {**original_query(symbol, cid), "status": "NEW"}
        with patch.object(self.f.broker, "submit", side_effect=pending_submit), patch.object(self.f.broker, "query", side_effect=pending_query):
            self.open()
        self.assertEqual(self.progress_now()["phase"], "waiting_open")
        self.assertIsNone(self.progress_now()["opened_at"])
        confirmed_after = time.time()
        self.executor.reconcile(self.f.account)
        self.assertGreaterEqual(self.progress_now()["opened_at"], confirmed_after)

    def test_actual_fill_amount_outside_range_rolls_back_and_backs_off(self):
        for scope, minimum, maximum, execution_price in (("per_side", "0", "10000", "6000"),
                                                          ("gross", "0", "18000", "4510"),
                                                          ("per_side", "8700", "10000", "4300")):
            with self.subTest(scope=scope, minimum=minimum):
                config = {**DEFAULT_CYCLE, "enabled": True, "min_notional": minimum, "max_notional": maximum, "notional_scope": scope}
                self.f.account["cycle"] = config
                self.f.store.save_account(self.f.account)
                progress = {**self.progress, "config": config}
                self.f.store.put("cycle:test", progress)
                original_book, original_submit = self.f.market.book, self.f.broker.submit
                execution_started = []
                def moved_book(symbol):
                    book = original_book(symbol)
                    if execution_started:
                        book.bid = book.mark = dec(execution_price)
                        book.ask = dec(execution_price) + dec("0.01")
                    return book
                def slipped(orders):
                    execution_started.append(True)
                    return original_submit(orders)
                with patch.object(self.f.market, "book", side_effect=moved_book), patch.object(self.f.broker, "submit", side_effect=slipped) as submit:
                    self.open()
                self.assertEqual(submit.call_count, 2)
                self.assertEqual(self.quantities(), (0, 0))
                result = self.progress_now()
                self.assertEqual(result["phase"], "waiting_open")
                self.assertEqual(result["failure_count"], 1)
                self.assertGreater(result["retry_at"], time.time() + 25)
                self.assertIsNone(result["opened_at"])
                self.assertIn("实际成交金额", result["reason"])

    def test_post_fill_margin_above_limit_rolls_back(self):
        original = self.f.broker.submit
        def withdrawal(orders):
            self.f.broker.state["wallet"] = "15000"
            self.f.broker.save()
            return original(orders)
        with patch.object(self.f.broker, "submit", side_effect=withdrawal) as submit:
            self.open()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(self.quantities(), (0, 0))
        self.assertIn("保证金占用", self.progress_now()["reason"])

    def test_close_is_allowed_with_nonpositive_equity(self):
        self.open()
        self.f.broker.state["wallet"] = "-1"
        self.f.broker.save()
        self.close_cycle()
        self.assertEqual(self.quantities(), (0, 0))
        self.assertEqual(self.progress_now()["completed_cycles"], 1)

    def test_absent_paper_repair_yields_without_recursive_unbounded_retry(self):
        original = self.f.broker.submit
        def uncommitted_repair(orders):
            if orders[0]["side"] == "BUY":
                return [original(orders[:1])[0], {"code": -2019}]
            raise AmbiguousOrder("simulated repair not committed")
        with patch.object(self.f.broker, "submit", side_effect=uncommitted_repair) as submit:
            self.open()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(self.f.store.intent("test")["repair_attempts"], 0)
        self.executor.reconcile(self.f.account)
        self.assertEqual(self.quantities(), (0, 0))

    def test_stale_run_identity_blocks_any_submission(self):
        progress = self.progress_now()
        progress["run_id"] = "outdated-run"
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not submit")), self.assertRaisesRegex(TradingError, "持久记录"):
            self.executor.start(self.f.account, self.f.broker.cycle_snapshot(["XAUUSD1"]), self.plan("open"), progress)

    def test_disabled_leverage_path_never_calls_admission_or_creates_intent(self):
        callback = Mock()
        with self.assertRaises(TradingError):
            self.executor.set_leverage(self.f.account, self.f.broker.cycle_snapshot(["XAUUSD1"]), self.progress_now(), before_submit=callback)
        callback.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))


class CycleLiveBrokerTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.api = Mock()
        self.api.budget = None
        self.live = LiveBroker({}, self.f.market, api=self.api)

    def test_cycle_snapshot_does_not_query_external_order_inventory(self):
        from .test_exchange_hardening import FixtureAPI, account_responses
        responses = account_responses()
        responses["/fapi/v3/openOrders"] = AssertionError("must not query external orders")
        self.live.api = FixtureAPI(responses)
        verified = self.live.cycle_snapshot(["XAUUSD1"])
        self.assertIsNone(verified.open_orders)
        calls = [call for call in self.live.api.calls if call[1] == "/fapi/v3/openOrders"]
        self.assertEqual(calls, [])
        self.assertIsNone(self.live.cycle_snapshot(["XAUUSD1"]).open_orders)

    def test_cycle_leverage_does_not_trust_old_flat_snapshot(self):
        old = self.f.broker.snapshot(["XAUUSD1"])
        changed = copy.deepcopy(old)
        changed.pair("XAUUSD1")[0].qty = dec(1)
        with patch.object(self.live, "cycle_snapshot", return_value=changed) as read, self.assertRaises(RequestNotSent):
            self.live.set_cycle_leverage("XAUUSD1", 2, checked_snapshot=old)
        read.assert_called_once_with(["XAUUSD1"], fresh_modes=True)
        self.api.call.assert_not_called()

    def test_cycle_leverage_accepts_2x_after_final_flat_position_check(self):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        self.api.call.return_value = {"symbol": "XAUUSD1", "leverage": 2}
        callback = Mock()
        with patch.object(self.live, "cycle_snapshot", return_value=snapshot):
            self.live.set_cycle_leverage("XAUUSD1", 2, before_submit=callback)
        callback.assert_called_once_with(snapshot)
        self.api.call.assert_called_once_with("POST", "/fapi/v3/leverage", {"symbol": "XAUUSD1", "leverage": "2"}, signed=True, weight=1)
        with self.assertRaises(TradingError):
            self.live.set_leverage("XAUUSD1", 2)

    def test_unknown_leverage_mutation_is_never_retried(self):
        snapshot = self.f.broker.snapshot(["XAUUSD1"])
        account = self.f.account
        account["cycle"] = {**DEFAULT_CYCLE, "enabled": True}
        self.f.store.save_account(account)
        progress = {"run_id": "live-leverage", "phase": "waiting_open", "quantities": {"LONG": "0", "SHORT": "0"},
                    "opened_at": None, "completed_cycles": 0, "config": account["cycle"]}
        self.f.store.put("cycle:test", progress)
        executor = CycleExecutor(self.f.store, self.live, self.f.market)
        with patch.object(self.live, "cycle_snapshot", return_value=snapshot), \
             patch.object(self.live, "set_cycle_leverage", side_effect=AmbiguousOrder("unknown")) as mutate:
            # Simulate an already submitted pre-upgrade leverage request.
            self.f.store.save_intent({"id": "legacy-leverage", "kind": "cycle_leverage", "account_id": "test",
                "run_id": progress["run_id"], "symbol": "XAUUSD1", "previous": 5, "target": 2,
                "status": "pending", "created_at": time.time(), "progress": progress})
            executor.reconcile(account)
            pending = self.f.store.intent("test")
            pending["created_at"] -= 130
            self.f.store.save_intent(pending)
            executor.reconcile(account)
        mutate.assert_not_called()
        self.assertEqual(self.f.store.intent("test")["status"], "attention")


if __name__ == "__main__":
    unittest.main()
