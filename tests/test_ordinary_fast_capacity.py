"""Ordinary 5x uses shared fast capacity samples, with normal execution guards."""
from contextlib import ExitStack
from copy import deepcopy
import time
import unittest
from unittest.mock import patch

from trading.cycle import DEFAULT_CYCLE
from trading.engine import Engine, snapshot_json
from trading.exchange import ExchangeError, BudgetWait
from trading.models import Plan, SYMBOLS, dec
from tests.helpers import Fixture, account
from tests.test_cycle_ws_scheduler import _Harness, _Future, _Pool


XAU, SPCX, CL = SYMBOLS


class OrdinaryFastCapacityTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.engine.view("test", snapshot=snapshot_json(self.f.broker.snapshot([XAU]), [XAU]))

    def targets(self, **policy):
        owner = self.f.store.account("test")
        owner["policy"].update(symbols=list(SYMBOLS), **policy)
        return self.engine.fast_capacity_targets([owner])

    def sample(self, values, tick=100, *, full=False):
        self.engine.capacity_targets = {XAU: {5}}
        self.engine.capacity_accounts = self.f.store.accounts()
        if full:
            self.engine.capacity_full_checked.pop(XAU, None)
        with patch("trading.engine.time.time", return_value=1_800_000_000 + tick), \
             patch("trading.engine.time.monotonic", return_value=tick), \
             patch.object(self.f.market, "capacities", return_value={tier: dec(value) for tier, value in values.items()}) as read:
            interval = self.engine.poll_market(XAU)
        return interval, read

    def priority_tick(self):
        work = self.engine.work("test")
        work.start_priority()
        return self.engine.tick_account("test")

    def live_sample(self, values):
        with patch.object(self.f.market, "capacities", return_value={tier: dec(value) for tier, value in values.items()}):
            self.engine.poll_market(XAU)

    def test_selected_market_or_all_have_fast_5x_without_a_cycle(self):
        for selected in SYMBOLS:
            with self.subTest(selected=selected):
                self.assertEqual(self.targets(ordinary_symbol=selected), {selected: {5}})
        self.assertEqual(self.targets(ordinary_symbol="all"), dict.fromkeys(SYMBOLS, {5}))
        self.assertEqual(self.targets(min_open_leverage=10), {})
        self.assertEqual(self.targets(min_open_leverage=20), {})

    def test_paused_disallowed_live_and_migration_accounts_do_not_add_fast_5x(self):
        owner = self.f.store.account("test")
        for changed in ({"enabled": False}, {"mode": "live"}, {"migration": {"enabled": True}}):
            with self.subTest(changed=changed), patch.dict("os.environ", {}, clear=True):
                self.assertEqual(self.engine.fast_capacity_targets([{**owner, **changed}]), {})

    def test_cycle_ownership_and_multiple_accounts_share_one_feed(self):
        owner = self.f.store.account("test")
        owner["policy"]["symbols"] = list(SYMBOLS)
        owner["cycle"] = {**DEFAULT_CYCLE, "enabled": True, "symbol": XAU}
        self.engine.view("test", snapshot={"timestamp": time.time(), "positions": [{"symbol": XAU, "leverage": 10}]})
        self.assertEqual(self.engine.fast_capacity_targets([owner]), {XAU: {10}, SPCX: {5}, CL: {5}})
        second = account("second")
        targets = self.engine.fast_capacity_targets([owner, second, deepcopy(second)])
        self.assertEqual(targets, {XAU: {5, 10}, SPCX: {5}, CL: {5}})

    def test_each_fast_sample_can_detect_recovery_without_duplicate_unchanged_work(self):
        self.sample({5: 0, 10: 0, 20: 0})
        self.assertFalse(self.engine.work("test").priority)
        interval, read = self.sample({5: 500000}, 100.2)
        self.assertEqual(interval, .2)
        read.assert_called_once_with(XAU, {5})
        self.assertIn(XAU, self.engine.work("test").take_priority())
        self.sample({5: 500000}, 100.4)
        self.assertFalse(self.engine.work("test").priority)
        self.sample({5: 10000}, 100.6)
        self.sample({5: 500000}, 100.8)
        self.assertIn(XAU, self.engine.work("test").priority)

    def test_partial_5x_does_not_remove_or_renew_a_high_tier_opportunity(self):
        self.sample({5: 0, 10: 500000, 20: 0})
        stamp = self.engine.work("test").priority[XAU]
        self.sample({5: 0}, 100.2)
        self.assertEqual(self.engine.work("test").priority_levels[XAU], (10,))
        self.assertEqual(self.engine.work("test").priority[XAU], stamp)
        self.assertEqual(self.engine.markets[XAU]["capacity_checked_at"]["10"], stamp)
        # Simulate a stale full-tier cache: fast 5x success must not revive it.
        self.engine.capacity_full_checked[XAU] = 108.2
        self.sample({5: 0}, 108.2)
        self.assertNotIn(XAU, self.engine.work("test").priority)
        self.assertNotIn(XAU, self.engine.work("test").priority_levels)

    def test_missing_5x_and_failed_requests_revoke_the_opportunity(self):
        self.sample({5: 500000})
        self.sample({}, 100.2)
        self.assertNotIn(XAU, self.engine.work("test").priority)
        self.sample({5: 500000}, 100.4)
        with patch.object(self.f.market, "capacities", side_effect=ExchangeError("rate limited", retry_after=180)):
            self.assertEqual(self.engine.poll_market(XAU), 180)
        self.assertNotIn(XAU, self.engine.work("test").priority)
        self.assertEqual(self.engine.markets[XAU]["status"], "error")

    def test_5x_cannot_wake_higher_floor_or_higher_actual_leverage(self):
        for leverage, floor in ((5, 10), (5, 20), (10, 5), (20, 5)):
            with self.subTest(leverage=leverage, floor=floor):
                owner = self.f.store.account("test")
                owner["policy"]["min_open_leverage"] = floor
                self.f.store.save_account(owner)
                self.engine.view("test", snapshot={"positions": [{"symbol": XAU, "leverage": leverage}]})
                self.live_sample({5: 500000, 10: 0, 20: 0})
                self.assertFalse(self.engine.work("test").priority)

    def test_5x_signal_respects_selection_cycle_ownership_and_threshold(self):
        owner = self.f.store.account("test")
        other = account("other")
        other["policy"]["symbols"] = list(SYMBOLS)
        other["policy"]["ordinary_symbol"] = CL
        owner["cycle"] = {**DEFAULT_CYCLE, "enabled": True, "symbol": XAU}
        self.engine.wake_capacity_accounts(XAU, {5: dec(500000)}, time.time(), [owner, other])
        self.assertFalse(self.engine.work("test").priority)
        self.assertFalse(self.engine.work("other").priority)
        self.engine.wake_capacity_accounts(CL, {5: dec(10000)}, time.time(), [other])
        self.assertFalse(self.engine.work("other").priority)
        self.engine.wake_capacity_accounts(CL, {5: dec("10000.01")}, time.time(), [other])
        self.assertIn(CL, self.engine.work("other").priority)

    def test_successful_5x_fill_rearms_only_on_the_next_sample(self):
        self.live_sample({5: 500000, 10: 0, 20: 0})
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.priority_tick()
            submit.assert_called_once()
            self.assertFalse(self.engine.work("test").followup)
            self.assertFalse(self.engine.work("test").priority)
            self.live_sample({5: 500000, 10: 0, 20: 0})
            self.assertIn(XAU, self.engine.work("test").priority)
            self.priority_tick()
            self.assertEqual(submit.call_count, 2)
        self.assertEqual(self.f.broker.state["leverages"][XAU], 5)

    def test_rejected_or_blocked_batch_does_not_rearm_on_unchanged_capacity(self):
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                self.live_sample({5: 0, 10: 0, 20: 0})
                self.live_sample({5: 500000, 10: 0, 20: 0})
                target = (patch.object(self.f.broker, "submit", return_value=[{"code": -5018}] * 2) if rejected else
                          patch("trading.engine.plan_pair", return_value=Plan()))
                with target:
                    self.priority_tick()
                self.live_sample({5: 500000, 10: 0, 20: 0})
                self.assertFalse(self.engine.work("test").priority)
                if rejected:
                    self.assertGreater(self.f.store.get("order_cooldown:test:" + XAU)["until"], time.time())

    def test_5x_upgrade_confirmation_continues_without_ordinary_wait(self):
        self.f.broker.state["leverages"][XAU] = 1
        self.f.broker.save()
        self.engine.view("test", snapshot=snapshot_json(self.f.broker.snapshot([XAU]), [XAU]))
        self.live_sample({5: 500000, 10: 0, 20: 0})
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.priority_tick()
            self.assertEqual(self.f.store.intent("test")["target"], 5)
            self.assertTrue(self.engine.work("test").followup)
            self.priority_tick()
            self.assertIsNone(self.f.store.intent("test"))
            self.assertTrue(self.engine.work("test").followup)
            self.priority_tick()
            submit.assert_called_once()

    def test_pending_confirmation_rearms_only_after_reconciliation(self):
        self.live_sample({5: 500000, 10: 0, 20: 0})
        with patch("trading.engine.Executor.reconcile", return_value="pending"):
            self.priority_tick()
        self.assertIsNotNone(self.f.store.intent("test"))
        self.live_sample({5: 500000, 10: 0, 20: 0})
        self.assertFalse(self.engine.work("test").priority)
        self.priority_tick()
        self.assertIsNone(self.f.store.intent("test"))
        self.live_sample({5: 500000, 10: 0, 20: 0})
        self.assertIn(XAU, self.engine.work("test").priority)

    def test_5x_budget_backoff_and_saved_rejection_cooldown_still_apply(self):
        self.live_sample({5: 500000, 10: 0, 20: 0})
        with patch.object(self.f.broker, "snapshot", side_effect=BudgetWait("budget", retry_after=45)):
            self.assertEqual(self.priority_tick(), 45)
        self.assertGreater(self.engine.work("test").backoff, time.monotonic())
        self.f.store.put("order_cooldown:test:" + XAU, {"until": time.time() + 120, "failures": 3})
        with patch.object(self.f.broker, "submit") as submit:
            self.priority_tick()
            submit.assert_not_called()

    def test_capacity_lost_during_candidate_checks_prevents_submission(self):
        self.live_sample({5: 500000, 10: 0, 20: 0})
        book = self.f.market.book

        def quote(symbol):
            result = book(symbol)
            self.live_sample({5: 0, 10: 0, 20: 0})
            return result

        with patch.object(self.f.market, "book", side_effect=quote), patch.object(self.f.broker, "submit") as submit:
            self.priority_tick()
            submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_post_fill_risk_failure_does_not_rearm(self):
        self.live_sample({5: 500000, 10: 0, 20: 0})
        with patch.object(self.engine, "check_post_fill_occupancy", return_value=True):
            self.priority_tick()
        self.assertTrue(self.f.broker.state["orders"])
        self.live_sample({5: 500000, 10: 0, 20: 0})
        self.assertFalse(self.engine.work("test").priority)


class OrdinaryFastSchedulerTests(unittest.TestCase):
    def setUp(self):
        f = Fixture()
        self.addCleanup(f.close)
        self.h = _Harness(f)
        owner = f.store.account("test")
        owner["cycle"]["enabled"] = False
        owner["policy"].update(symbols=[XAU], ordinary_symbol=XAU)
        f.store.save_account(owner)

    def run_sampling(self, poll, control):
        h = self.h
        h.control = control
        with ExitStack() as stack:
            stack.enter_context(patch("trading.engine.ThreadPoolExecutor", return_value=_Pool()))
            stack.enter_context(patch("trading.engine.time.monotonic", side_effect=lambda: h.ticks))
            stack.enter_context(patch("trading.engine.time.time", side_effect=lambda: h.wall))
            stack.enter_context(patch.object(h.engine, "scheduling", return_value=h.timing))
            stack.enter_context(patch.object(h.engine, "tick_account", return_value=60))
            stack.enter_context(patch.object(h.engine, "poll_market", side_effect=poll))
            for name in ("poll_book", "poll_depth", "poll_public_brackets", "notify"):
                stack.enter_context(patch.object(h.engine, name, return_value=60))
            h.engine.run()
        if h.failure:
            raise h.failure

    def test_selected_ordinary_symbol_starts_every_200ms_including_request_latency(self):
        h, starts = self.h, []

        def poll(symbol):
            starts.append((symbol, h.ticks))
            if symbol == XAU:
                h.ticks += .12
                return .2
            return 2

        def control(step):
            if step < 3:
                h.ticks = 100.199 if step == 1 else 100.201
            else:
                h.engine.shutdown.set()

        self.run_sampling(poll, control)
        self.assertEqual([round(t, 3) for s, t in starts if s == XAU], [100, 100.201])
        self.assertEqual(len([s for s, _ in starts if s != XAU]), 2)

    def test_slow_ordinary_sample_does_not_overlap_or_catch_up(self):
        h, starts, held = self.h, [], _Future(.2, done=False)

        def poll(symbol):
            if symbol != XAU:
                return 2
            starts.append(h.ticks)
            return held if len(starts) == 1 else .2

        def control(step):
            h.ticks = 100 + step * .5
            if step == 2:
                held.complete = True
            if step == 3:
                h.engine.shutdown.set()

        self.run_sampling(poll, control)
        self.assertEqual(starts, [100, 101])

    def test_selection_pause_and_minimum_changes_update_fast_targets(self):
        h, targets = self.h, []

        def poll(symbol):
            return .2 if symbol in h.engine.capacity_targets else 2

        def control(step):
            targets.append(deepcopy(h.engine.capacity_targets))
            owner = h.fixture.store.account("test")
            if step == 1:
                owner["policy"].update(symbols=list(SYMBOLS), ordinary_symbol=CL)
            elif step in (2, 3):
                owner["enabled"] = step == 3
            elif step == 4:
                owner["policy"]["min_open_leverage"] = 10
            else:
                h.engine.shutdown.set()
                return
            h.fixture.store.save_account(owner)
            h.engine.accounts_generation += 1
            h.ticks += .3

        self.run_sampling(poll, control)
        self.assertEqual(targets, [{XAU: {5}}, {CL: {5}}, {}, {CL: {5}}, {}])

    def test_5x_opportunity_bypasses_interval_and_gap_after_normal_turn(self):
        h = self.h
        owner = h.fixture.store.account("test")

        def control(step):
            if step == 1:
                h.ticks = 100.2
                h.engine.markets[XAU] = {"status": "ok", "checked_at": h.wall, "capacities": {"5": "500000"}}
                h.engine.wake_capacity_accounts(XAU, {5: dec(500000)}, h.wall, [owner])
            else:
                h.engine.shutdown.set()

        h.run(control)
        self.assertEqual([call["at"] for call in h.calls], [100, 100.2])
        self.assertEqual(h.calls[1]["priority"], {XAU: h.wall})


if __name__ == "__main__":
    unittest.main()
