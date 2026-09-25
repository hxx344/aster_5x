"""Sustained ordinary opportunities must fit the shared request budget."""
import unittest
from unittest.mock import patch

import httpx

from tests.helpers import Fixture, account
from tests.test_cycle_ws_scheduler import _Harness
from tests import test_ordinary_fast_capacity as ordinary_fast
from trading.engine import Engine, PUBLIC_POLL_ALLOWANCE, CAPACITY_MONITOR_RESERVE
from trading.exchange import API, LiveBroker, MarketData, RateBudget
from trading.models import SYMBOLS, dec
from trading.scheduling import PollBackoff

XAU = SYMBOLS[0]


class PublicSamplingBudgetTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.budget = RateBudget()
        self.api = API(budget=self.budget)
        self.addCleanup(self.api.close)
        self.engine = Engine(self.f.store, market=MarketData(self.api))

    def configure(self, count):
        self.engine.capacity_targets = dict.fromkeys(SYMBOLS[:count], {5})
        intervals, brackets, enabled = self.engine.capacity_poll_schedule(self.engine.capacity_targets)
        self.engine.capacity_intervals = intervals
        self.engine.capacity_brackets_interval = brackets
        self.engine.capacity_poll_enabled = enabled
        return intervals, brackets, enabled

    def test_single_symbol_keeps_200ms_but_all_symbols_fit_actual_reserve(self):
        for count in range(4):
            with self.subTest(count=count):
                intervals, brackets, enabled = self.configure(count)
                cost = sum(60 / value for value in intervals.values()) + 3 * 60 / brackets
                self.assertTrue(enabled)
                self.assertLessEqual(cost, self.budget.snapshot()["capacity_reserve"] + 1e-6)
                if count == 1:
                    self.assertEqual(intervals[XAU], .2)
                    self.assertEqual(intervals[SYMBOLS[1]], 2)
                if count > 1:
                    self.assertGreater(intervals[XAU], .2)

    def test_all_symbol_cost_no_longer_reduces_private_schedule_to_one_weight(self):
        intervals, brackets, _ = self.configure(3)
        owners = [account("a" + str(i), mode="live") for i in range(8)]
        for owner in owners:
            self.engine.view(owner["id"], snapshot={"positions": [
                {"symbol": XAU, "side": side, "leverage": 5, "qty": "0"} for side in ("LONG", "SHORT")]})
        schedules = self.engine.scheduling(owners)
        public = PUBLIC_POLL_ALLOWANCE - CAPACITY_MONITOR_RESERVE + sum(60 / v for v in intervals.values()) + 180 / brackets
        total = public + sum(120 * 60 / schedules[o["id"]]["interval"] for o in owners)
        self.assertLessEqual(total, self.budget.snapshot()["execution_limit"] + 1e-6)
        self.assertLess(schedules["a0"]["interval"], 300)

    def test_low_limits_slow_baseline_and_zero_reserve_disables_public_io(self):
        for limit in (900, 120, 6, 1):
            with self.subTest(limit=limit):
                self.budget.limit = limit
                intervals, brackets, enabled = self.configure(3)
                reserve = self.budget.snapshot()["capacity_reserve"]
                if reserve:
                    self.assertTrue(enabled)
                    self.assertLessEqual(sum(60 / v for v in intervals.values()) + 180 / brackets, reserve + 1e-6)
                else:
                    self.assertFalse(enabled)
                    with patch.object(self.engine.market, "capacities") as oi, patch.object(self.engine.market, "refresh_public_brackets") as tiers:
                        self.engine.poll_market(XAU)
                        self.engine.poll_public_brackets(XAU)
                    oi.assert_not_called()
                    tiers.assert_not_called()

    def test_insufficient_whole_batch_budget_prevents_any_order_intent_or_http(self):
        requests = []
        api = API(budget=RateBudget(), transport=httpx.MockTransport(lambda req: requests.append(req) or httpx.Response(200, json={})))
        self.addCleanup(api.close)
        engine = Engine(self.f.store, market=self.f.market)
        broker = LiveBroker({}, self.f.market, api)
        engine.brokers["test"] = broker
        room = broker.snapshot_weight([XAU])
        api.budget.reserve(1500 - room)
        engine.poll_market(XAU)
        engine.markets[XAU]["capacities"] = {"5": "500000"}
        with patch.object(broker, "snapshot", side_effect=self.f.broker.snapshot):
            engine.tick_account("test")
        self.assertEqual(requests, [])
        self.assertIsNone(self.f.store.intent("test"))
        self.assertIn("预算", engine.views["test"]["reason"])


class OrdinaryPriorityPacingTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.h = _Harness(self.f)
        self.owner = self.f.store.account("test")
        self.owner["cycle"]["enabled"] = False
        self.owner["policy"].update(symbols=[XAU], ordinary_symbol=XAU)
        self.f.store.save_account(self.owner)

    def signal(self, values=None):
        h = self.h
        values = values or {5: dec(500000)}
        h.engine.markets[XAU] = {"status": "ok", "checked_at": h.wall, "capacities": {str(k): str(v) for k, v in values.items()}}
        h.engine.wake_capacity_accounts(XAU, values, h.wall, [self.owner])

    def test_flapping_and_success_rearm_cannot_restart_private_work_every_sample(self):
        h = self.h
        def control(step):
            moments = (100.2, 100.4, 110.2, 130.1, 130.201)
            if step > len(moments):
                h.engine.shutdown.set()
                return
            h.ticks = moments[step - 1]
            if step == 2:
                self.signal({5: dec(0)})
            if step == 3:
                h.engine.work("test").priority_levels.pop(XAU, None)
            self.signal()
        h.run(control)
        self.assertEqual([r["at"] for r in h.calls], [100, 100.2, 130.201])

    def test_budget_retry_followup_obeys_pacing_as_well_as_hard_backoff(self):
        h = self.h
        def control(step):
            if step == 1:
                h.ticks = 100.2
                self.signal()
            elif step == 2:
                h.ticks = 110.3
                work = h.engine.work("test")
                work.backoff = 110.2
                h.engine.priority_continuation("test")
            elif step == 3:
                h.ticks = 130.3
            else:
                h.engine.shutdown.set()
        h.run(control)
        self.assertEqual([r["at"] for r in h.calls], [100, 100.2, 130.3])

    def test_higher_tier_and_leverage_continuation_remain_immediate(self):
        h = self.h
        def control(step):
            if step == 1:
                h.ticks = 100.2
                self.signal()
            elif step == 2:
                h.ticks = 100.4
                h.engine.priority_continuation("test", leverage=True)
            elif step == 3:
                h.ticks = 100.6
                self.signal({5: dec(500000), 10: dec(500000)})
            else:
                h.engine.shutdown.set()
        h.run(control)
        self.assertEqual([r["at"] for r in h.calls], [100, 100.2, 100.4, 100.6])

    def test_pending_order_recovery_is_not_delayed_by_priority_pacing(self):
        h = self.h
        def control(step):
            if step == 1:
                h.ticks = 100.2
                self.signal()
            elif step == 2:
                h.ticks = 100.4
                work = h.engine.work("test")
                work.urgent = work.wake = True
            else:
                h.engine.shutdown.set()
        h.run(control)
        self.assertEqual([r["at"] for r in h.calls], [100, 100.2, 100.4])

    def test_slow_failure_keeps_full_backoff_after_sampling_budget_recovers(self):
        h, starts = self.h, []
        h.api.budget = RateBudget()
        h.api.budget.limit = 120  # monitoring reserve 33; all baseline polls slow down
        def poll(symbol):
            if symbol != XAU:
                return h.engine.capacity_interval(symbol)
            starts.append(h.ticks)
            if len(starts) == 1:
                h.ticks += 12
                return PollBackoff(10)
            return h.engine.capacity_interval(symbol)
        def control(step):
            if step == 1:
                # Raise the configured interval even beyond the retry delay,
                # reproducing the old numeric-success classification bug.
                h.engine.capacity_intervals[XAU] = 18.6
            elif step == 2:
                h.api.budget.limit = 1800
                h.ticks = 113
            elif step == 3:
                h.ticks = 121.9
            elif step == 4:
                h.ticks = 122.1
            else:
                h.engine.shutdown.set()
        ordinary_fast.OrdinaryFastSchedulerTests.run_sampling(self, poll, control)
        self.assertEqual(starts, [100, 122.1])


if __name__ == "__main__":
    unittest.main()
