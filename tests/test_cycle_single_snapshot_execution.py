"""A prepared cycle admits one fresh account read without weakening execution."""
import copy
from dataclasses import replace
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trading.cycle_execution import CycleExecutor
from trading.models import TradingError, dec
from tests import test_cycle_execution as execution_cases


class CycleSingleSnapshotExecutionTests(unittest.TestCase):
    setUp = execution_cases.CycleExecutionTests.setUp
    plan = execution_cases.CycleExecutionTests.plan
    progress_now = execution_cases.CycleExecutionTests.progress_now
    quantities = execution_cases.CycleExecutionTests.quantities

    def prepared_start(self, phase="open", **options):
        snapshot = self.executor.prepare_snapshot(self.f.account)
        return self.executor.start(self.f.account, snapshot, self.plan(phase), self.progress_now(), **options)

    def stop_at_callback(self, snapshot, *, executor=None, account=None):
        executor = executor or self.executor
        account = account or self.f.account
        callback = Mock(side_effect=TradingError("stop at final check"))
        with patch.object(self.f.broker, "cycle_snapshot", wraps=self.f.broker.cycle_snapshot) as read, \
             patch.object(self.f.broker, "submit") as submit, \
             self.assertRaisesRegex(TradingError, "stop at final check"):
            executor.start(account, snapshot, self.plan("open"), self.progress_now(), before_submit=callback)
        callback.assert_called_once()
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent(account["id"]))
        return read.call_args_list

    def test_real_open_and_close_read_once_before_each_submit_and_reconcile_afterward(self):
        original = self.f.broker.submit
        for phase, expected in (("open", (2, 2)), ("close", (0, 0))):
            with self.subTest(phase=phase):
                if phase == "close":
                    progress = self.progress_now()
                    progress.update(opened_at=time.time() - 61, phase="waiting_close")
                    self.f.store.put("cycle:test", progress)
                observed = []
                with patch.object(self.f.broker, "cycle_snapshot", wraps=self.f.broker.cycle_snapshot) as read:
                    def submit(orders):
                        self.assertEqual(read.call_count, 1)
                        self.assertEqual(read.call_args.args, (["XAUUSD1"],))
                        self.assertEqual(read.call_args.kwargs, {"fresh_modes": True})
                        self.assertEqual(self.f.store.intent("test")["status"], "pending")
                        return original(orders)
                    with patch.object(self.f.broker, "submit", side_effect=submit) as sent:
                        snapshot = self.executor.prepare_snapshot(self.f.account)
                        self.executor.start(self.f.account, snapshot, self.plan(phase), self.progress_now(),
                                            before_submit=observed.append)
                    sent.assert_called_once()
                    # The second read confirms the fills; it cannot precede POST.
                    self.assertEqual(read.call_count, 2)
                    self.assertIs(observed[0], snapshot)
                self.assertEqual(self.quantities(), expected)
                self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.progress_now()["completed_cycles"], 1)

    def test_expired_admission_never_refreshes_or_creates_an_intent(self):
        with patch("trading.cycle_execution.monotonic", side_effect=[100, 108.001]), \
             patch.object(self.f.broker, "cycle_snapshot", wraps=self.f.broker.cycle_snapshot) as read, \
             patch.object(self.f.broker, "submit") as submit:
            snapshot = self.executor.prepare_snapshot(self.f.account)
            with self.assertRaisesRegex(TradingError, "本轮账户快照已过期"):
                self.executor.start(self.f.account, snapshot, self.plan("open"), self.progress_now())
        self.assertEqual(read.call_count, 1)
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_slow_final_callback_cannot_renew_admission_by_updating_wall_timestamp(self):
        def slow_callback(snapshot):
            snapshot.timestamp = time.time()
        with patch("trading.cycle_execution.monotonic", side_effect=[100, 100.1, 108.001]), \
             patch.object(self.f.broker, "cycle_snapshot", wraps=self.f.broker.cycle_snapshot) as read, \
             patch.object(self.f.broker, "submit") as submit, \
             self.assertRaisesRegex(TradingError, "本轮账户快照已过期"):
            self.prepared_start(before_submit=slow_callback)
        self.assertEqual(read.call_count, 1)
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_cap_expiring_during_final_callback_blocks_a_still_fresh_account_admission(self):
        now = [104.9]
        snapshot = replace(self.f.broker.cycle_snapshot(["XAUUSD1"]),
                           cycle_cap_cached_at={"XAUUSD1": 100.0})
        checked = []
        def final_callback(fresh):
            self.assertIs(fresh, snapshot)
            fresh.require_fresh()
            checked.append(now[0])
            now[0] = 105.1
        with patch("trading.models.time.monotonic", side_effect=lambda: now[0]), \
             patch("trading.cycle_execution.monotonic", side_effect=lambda: now[0]), \
             patch.object(self.f.broker, "cycle_snapshot", return_value=snapshot) as read, \
             patch.object(self.f.broker, "submit") as submit, \
             patch.object(self.f.store, "create_cycle_intent", wraps=self.f.store.create_cycle_intent) as persist, \
             self.assertRaisesRegex(TradingError, "风控档位查询已过期"):
            self.prepared_start(before_submit=final_callback)
        self.assertEqual(checked, [104.9])
        read.assert_called_once_with(["XAUUSD1"], fresh_modes=True)
        persist.assert_not_called()
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_admission_clock_includes_the_account_request_itself(self):
        now = [100.0]
        original = self.f.broker.cycle_snapshot
        def slow_read(symbols, **options):
            now[0] += 8.1
            return original(symbols, **options)
        with patch("trading.cycle_execution.monotonic", side_effect=lambda: now[0]), \
             patch.object(self.f.broker, "cycle_snapshot", side_effect=slow_read) as read, \
             patch.object(self.f.broker, "submit") as submit, \
             self.assertRaisesRegex(TradingError, "本轮账户快照已过期"):
            self.prepared_start()
        self.assertEqual(read.call_count, 1)
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_expired_wall_timestamp_still_blocks_a_new_monotonic_admission(self):
        snapshot = self.f.broker.cycle_snapshot(["XAUUSD1"])
        snapshot.timestamp -= 9
        with patch.object(self.f.broker, "cycle_snapshot", return_value=snapshot) as read, \
             patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(TradingError, "账户快照已过期"):
            self.prepared_start()
        self.assertEqual(read.call_count, 1)
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_copied_snapshot_uses_legacy_fresh_read_and_consumes_the_original_proof(self):
        snapshot = self.executor.prepare_snapshot(self.f.account)
        first = self.stop_at_callback(copy.deepcopy(snapshot))
        second = self.stop_at_callback(snapshot)
        for calls in (first, second):
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0].kwargs, {"fresh_modes": True})

    def test_a_valid_proof_can_be_consumed_only_once_even_when_the_callback_stops(self):
        snapshot = self.executor.prepare_snapshot(self.f.account)
        self.assertEqual(self.stop_at_callback(snapshot), [])
        reused = self.stop_at_callback(snapshot)
        self.assertEqual(len(reused), 1)
        self.assertEqual(reused[0].kwargs, {"fresh_modes": True})

    def test_another_executor_cannot_reuse_the_preparing_executors_admission(self):
        snapshot = self.executor.prepare_snapshot(self.f.account)
        other = CycleExecutor(self.f.store, self.f.broker, self.f.market)
        calls = self.stop_at_callback(snapshot, executor=other)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs, {"fresh_modes": True})

    def test_proof_bound_to_another_account_cannot_skip_the_fresh_read(self):
        other_account = {**self.f.account, "id": "other"}
        snapshot = self.executor.prepare_snapshot(other_account)
        calls = self.stop_at_callback(snapshot)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs, {"fresh_modes": True})

    def test_proof_bound_to_another_symbol_cannot_skip_the_fresh_read(self):
        snapshot = self.f.broker.cycle_snapshot(["XAUUSD1"])
        with patch.object(self.f.broker, "cycle_snapshot", return_value=snapshot) as read:
            self.assertIs(self.executor.prepare_snapshot(self.f.account, "CLUSD1"), snapshot)
        read.assert_called_once_with(["CLUSD1"], fresh_modes=True)
        calls = self.stop_at_callback(snapshot)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs, {"fresh_modes": True})

    def test_failed_start_consumes_admission_before_any_early_guard(self):
        snapshot = self.executor.prepare_snapshot(self.f.account)
        progress = {**self.progress_now(), "run_id": "old-run"}
        with patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(TradingError, "持久记录"):
            self.executor.start(self.f.account, snapshot, self.plan("open"), progress)
        submit.assert_not_called()
        calls = self.stop_at_callback(snapshot)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs, {"fresh_modes": True})

    def test_failed_reprepare_invalidates_the_previous_admission(self):
        snapshot = self.executor.prepare_snapshot(self.f.account)
        with patch.object(self.f.broker, "cycle_snapshot", side_effect=TradingError("read failed")), \
             self.assertRaisesRegex(TradingError, "read failed"):
            self.executor.prepare_snapshot(self.f.account)
        calls = self.stop_at_callback(snapshot)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].kwargs, {"fresh_modes": True})

    def test_reconciliation_and_leverage_entry_invalidate_prepared_admission(self):
        for action in (lambda snapshot: self.executor.reconcile(self.f.account),
                       lambda snapshot: self.executor.set_leverage(self.f.account, snapshot, self.progress_now())):
            with self.subTest(action=action):
                snapshot = self.executor.prepare_snapshot(self.f.account)
                try:
                    action(snapshot)
                except TradingError as exc:
                    self.assertIn("禁止通过循环修改杠杆", str(exc))
                calls = self.stop_at_callback(snapshot)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0].kwargs, {"fresh_modes": True})

    def test_invalid_state_in_the_only_read_blocks_submission(self):
        baseline = self.f.broker.cycle_snapshot(["XAUUSD1"])
        cases = (
            ({"can_trade": False}, "交易权限"),
            ({"hedge_mode": False}, "双向持仓"),
            ({"multi_assets": True}, "单币保证金"),
            ({"equity": dec(0)}, "总权益不足"),
        )
        for changes, message in cases:
            with self.subTest(changes=changes), \
                 patch.object(self.f.broker, "cycle_snapshot", return_value=replace(baseline, **changes)) as read, \
                 patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(TradingError, message):
                self.prepared_start()
            self.assertEqual(read.call_count, 1)
            submit.assert_not_called()
            self.assertIsNone(self.f.store.intent("test"))
        for field, value, message in (("isolated", True, "全仓保证金"), ("qty", dec(-1), "持仓数量"),
                                      ("leverage", 3, "计划与本轮配置不一致")):
            snapshot = copy.deepcopy(baseline)
            for position in snapshot.pair("XAUUSD1"):
                setattr(position, field, value)
            with self.subTest(field=field), patch.object(self.f.broker, "cycle_snapshot", return_value=snapshot) as read, \
                 patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(TradingError, message):
                self.prepared_start()
            self.assertEqual(read.call_count, 1)
            submit.assert_not_called()
            self.assertIsNone(self.f.store.intent("test"))

    def test_external_close_quantity_seen_in_the_only_read_is_never_adopted(self):
        self.prepared_start()
        progress = self.progress_now()
        progress.update(opened_at=time.time() - 61, phase="waiting_close")
        self.f.store.put("cycle:test", progress)
        self.f.broker.state["positions"]["XAUUSD1:LONG"]["qty"] = "3"
        self.f.broker.save()
        with patch.object(self.f.broker, "cycle_snapshot", wraps=self.f.broker.cycle_snapshot) as read, \
             patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(TradingError, "循环实际多空数量与记录不一致"):
            self.prepared_start("close")
        self.assertEqual(read.call_count, 1)
        submit.assert_not_called()
        self.assertEqual(self.quantities(), (3, 2))
        self.assertIsNone(self.f.store.intent("test"))

    def test_local_account_review_records_its_actual_duration_without_another_read(self):
        trigger = {"source": "bbo", "received_at": time.time(), "received_monotonic": 100,
                   "pre_submit": {"queue_ms": 30, "initial_account_ms": 80, "planning_ms": 40}}
        timer = SimpleNamespace(monotonic=Mock(side_effect=[100.2, 100.203, 100.21, 100.22,
                                                           100.23, 100.26, 100.4, 100.45]), time=time.time)
        with patch("trading.cycle_quality.time", timer), \
             patch.object(self.f.broker, "cycle_snapshot", wraps=self.f.broker.cycle_snapshot) as read:
            self.prepared_start(trigger=trigger, before_submit=lambda snapshot: {})
        self.assertEqual(read.call_count, 2)  # One admission read, one fill reconciliation.
        quality = self.f.store.get("cycle_execution:test")
        parts = quality["timing"]["pre_submit"]
        self.assertAlmostEqual(parts["final_account_ms"], 3)
        self.assertAlmostEqual(parts["initial_account_ms"], 80)
        self.assertAlmostEqual(parts["other_ms"], 207)
        self.assertAlmostEqual(sum(parts.values()), quality["timing"]["trigger_to_request_ms"])

    def test_observation_clock_failure_does_not_weaken_real_admission_checks(self):
        timer = SimpleNamespace(monotonic=Mock(side_effect=RuntimeError("observation clock failed")), time=time.time)
        with patch("trading.cycle_quality.time", timer), \
             patch("trading.cycle_execution.monotonic", side_effect=[100, 109]), \
             patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(TradingError, "本轮账户快照已过期"):
            self.prepared_start()
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_invalid_monotonic_age_never_authorizes_submission(self):
        for started, checked in ((100, 99), (float("nan"), 100), (100, float("inf")), (True, 100)):
            with self.subTest(started=started, checked=checked), \
                 patch("trading.cycle_execution.monotonic", side_effect=[started, checked]), \
                 patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(TradingError, "本轮账户快照已过期"):
                self.prepared_start()
            submit.assert_not_called()
            self.assertIsNone(self.f.store.intent("test"))

    def test_close_keeps_nonpositive_equity_reduction_and_hold_guard(self):
        self.prepared_start()
        with patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(TradingError, "持仓时间"):
            self.prepared_start("close")
        submit.assert_not_called()
        self.f.broker.state["wallet"] = "-1"
        self.f.broker.save()
        progress = self.progress_now()
        progress.update(opened_at=time.time() - 61, phase="waiting_close")
        self.f.store.put("cycle:test", progress)
        self.prepared_start("close")
        self.assertEqual(self.quantities(), (0, 0))
        self.assertEqual(self.progress_now()["completed_cycles"], 1)

    def test_pause_during_final_callback_stops_the_prepared_batch_before_persistence(self):
        def pause(snapshot):
            self.f.store.pause_account(self.f.store.account("test"), "pause during final check")
        with patch.object(self.f.broker, "cycle_snapshot", wraps=self.f.broker.cycle_snapshot) as read, \
             patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(TradingError, "已暂停"):
            self.prepared_start(before_submit=pause)
        self.assertEqual(read.call_count, 1)
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_prepared_partial_open_repairs_only_the_confirmed_fill(self):
        original = self.f.broker.submit
        def partial(orders):
            return [original(orders[:1])[0], {"code": -2019}] if len(orders) == 2 else original(orders)
        with patch.object(self.f.broker, "submit", side_effect=partial) as submit:
            self.prepared_start()
        self.assertEqual(submit.call_count, 2)
        repair = submit.call_args_list[1].args[0]
        self.assertEqual([(order["positionSide"], order["side"], order["quantity"]) for order in repair],
                         [("LONG", "SELL", "2")])
        self.assertEqual(self.quantities(), (0, 0))
        self.assertEqual(self.progress_now()["phase"], "waiting_open")
        self.assertEqual(self.progress_now()["completed_cycles"], 0)

    def test_pending_batch_blocks_another_prepared_start_and_restart_does_not_resend(self):
        with patch.object(self.executor, "reconcile", return_value="before fill reconciliation"):
            self.prepared_start()
        snapshot = self.executor.prepare_snapshot(self.f.account)
        with patch.object(self.f.broker, "submit") as submit, self.assertRaisesRegex(TradingError, "已有批次"):
            self.executor.start(self.f.account, snapshot, self.plan("open"), self.progress_now())
        submit.assert_not_called()
        restored = CycleExecutor(self.f.store, self.f.broker, self.f.market)
        with patch.object(self.f.broker, "submit") as submit:
            restored.reconcile(self.f.account)
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.progress_now()["phase"], "holding")


if __name__ == "__main__":
    unittest.main()
