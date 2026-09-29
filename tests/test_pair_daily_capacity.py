"""Completed UTC daily pair targets yield only their own fast quota demand."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from tests import test_pair_capacity_cadence as cadence
from tests.helpers import Fixture, account
from tests.test_cycle_ws_scheduler import _Harness, WALL
from trading.engine import Engine
from trading.exchange import RateBudget
from trading.models import SYMBOLS, dec
from trading.pairing import pair_active
from trading.scheduling import PollBackoff
from trading.store import Store


XAU = SYMBOLS[0]
DAY = datetime.fromtimestamp(WALL, timezone.utc).date().isoformat()


def save_cycle_pair(store, pair_id="gold", long_id="test", short_id="second"):
    pair = cadence.save_ordinary_pair(store, pair_id, long_id, short_id)
    pair["ordinary"]["enabled"] = False
    pair["cycle"].update(enabled=True, daily_volume_limit="1000")
    return store.save_pair(pair)


def runtime(long="0", short="0", *, now=WALL, leverage=7):
    return {"daily_volume": {DAY: {"long": long, "short": short}},
            "snapshots": {side: {"timestamp": now,
                                "positions": [{"symbol": XAU, "leverage": leverage}]}
                          for side in ("long", "short")}}


class PairDailyCapacityTests(unittest.TestCase):
    targets = cadence.PairCapacityCadenceTests.targets
    configure = cadence.PairCapacityCadenceTests.configure
    install_budget = cadence.PairCapacityCadenceTests.install_budget

    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.pair = save_cycle_pair(self.f.store)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.addCleanup(self.engine.dashboard_reports.close)
        self.wall = WALL
        self.clock = patch("trading.engine.time.time", side_effect=lambda: self.wall)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.save_runtime()

    def save_runtime(self, long="0", short="0", **changes):
        state = {**runtime(long, short, now=self.wall), **changes}
        self.f.store.put("pair_runtime:gold", state)
        return state

    def test_both_sides_must_reach_exact_daily_boundary(self):
        for long, short, completed in (("999.99999999", "1000", False),
                                       ("1000", "999.99999999", False),
                                       ("1000", "1000", True),
                                       ("1000.00000001", "2500", True)):
            with self.subTest(long=long, short=short):
                self.save_runtime(long, short)
                self.assertEqual(self.targets(), {} if completed else {XAU: {7}})
                self.assertEqual(self.engine.capacity_market_symbols(), {XAU})
                self.assertEqual(self.f.store.pair("gold"), self.pair)

    def test_missing_unknown_and_invalid_ledger_or_limit_never_pause(self):
        valid = runtime("1000", "1000")
        cases = [{**valid, "volume_unknown": True}]
        cases.extend({**valid, "daily_volume": value} for value in (None, [], {}, {DAY: None}, {DAY: []}))
        for side in ("long", "short"):
            missing = deepcopy(valid)
            del missing["daily_volume"][DAY][side]
            cases.append(missing)
            for value in (None, True, False, "-1", "NaN", "Infinity", "bad", [], {}):
                state = deepcopy(valid)
                state["daily_volume"][DAY][side] = value
                cases.append(state)
        for index, state in enumerate(cases):
            with self.subTest(ledger=index):
                self.f.store.put("pair_runtime:gold", state)
                self.assertEqual(self.targets(), {XAU: {7}})
        self.f.store.put("pair_runtime:gold", valid)
        for value in ("0", None, True, "-1", "NaN", "Infinity", "invalid"):
            with self.subTest(limit=value):
                pair = deepcopy(self.pair)
                pair["cycle"]["daily_volume_limit"] = value
                self.assertEqual(self.engine.fast_capacity_targets(self.f.store.accounts(), [pair]), {XAU: {7}})
        pair = deepcopy(self.pair)
        del pair["cycle"]["daily_volume_limit"]
        self.assertEqual(self.engine.fast_capacity_targets(self.f.store.accounts(), [pair]), {XAU: {7}})

    def test_utc_midnight_restores_without_clearing_yesterdays_ledger(self):
        midnight = datetime.fromtimestamp(WALL, timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp() + 86400
        self.wall = midnight - .001
        state = self.save_runtime("1000", "1000")
        self.assertEqual(self.targets(), {})
        self.wall = midnight
        self.assertEqual(self.targets(), {XAU: {7}})
        self.assertEqual(self.f.store.get("pair_runtime:gold"), state)

    def test_increased_disabled_or_corrected_daily_target_restores_immediately(self):
        self.save_runtime("1000", "1000")
        self.assertEqual(self.targets(), {})
        for limit, completed in (("2000", False), ("0", False), ("1000", True), ("500", True)):
            with self.subTest(limit=limit):
                pair = self.f.store.pair("gold")
                pair["cycle"]["daily_volume_limit"] = limit
                self.f.store.save_pair(pair)
                self.assertEqual(self.targets(), {} if completed else {XAU: {7}})
        self.save_runtime("499.999", "1000")
        self.assertEqual(self.targets(), {XAU: {7}})

    def test_restart_reconstructs_completion_from_persisted_volume(self):
        state = self.save_runtime("1000", "1000")
        reopened = Store(self.f.store.path)
        restarted = Engine(reopened, market=self.f.market)
        self.addCleanup(restarted.dashboard_reports.close)
        self.assertEqual(restarted.fast_capacity_targets(reopened.accounts()), {})
        self.assertEqual(restarted.capacity_market_symbols(), {XAU})
        self.assertEqual(reopened.get("pair_runtime:gold"), state)

    def test_shared_ordinary_independent_and_unfinished_cycle_keep_their_tiers(self):
        self.save_runtime("1000", "1000")
        independent = account("independent")
        self.f.store.save_account(independent)
        self.assertEqual(self.targets(), {XAU: {5}})
        self.f.store.save_account({**independent, "enabled": False})
        other = cadence.save_ordinary_pair(self.f.store, "other", "third", "fourth")
        self.assertEqual(self.targets(), {XAU: {5, 10, 20}})
        other["ordinary"]["enabled"] = False
        other["cycle"].update(enabled=True, daily_volume_limit="1000")
        self.f.store.save_pair(other)
        self.f.store.put("pair_runtime:other", runtime("1000", "999", leverage=10))
        self.assertEqual(self.targets(), {XAU: {10}})

    def test_daily_completion_keeps_close_recovery_and_transfer_scheduling_active(self):
        for pending, quantities, margin in ((None, {"LONG": "1", "SHORT": "1"}, {}),
                                            ({"id": "close", "phase": "close"}, {}, {}),
                                            (None, {}, {"pending": {"id": "transfer"}})):
            with self.subTest(pending=pending, quantities=quantities, margin=margin):
                state = self.save_runtime("1000", "1000", pending=pending,
                                          progress={"phase": "waiting_close", "quantities": quantities})
                self.f.store.put("pair_margin:gold", margin)
                self.assertEqual(self.targets(), {})
                self.assertTrue(pair_active(self.pair, state))
                self.assertEqual(self.engine.pairs.poll_interval(self.pair, state), 1)
                self.assertEqual(self.f.store.get("pair_runtime:gold"), state)
                self.assertEqual(self.f.store.get("pair_margin:gold"), margin)

    def test_budget_releases_fast_reserve_without_refunding_spent_weight(self):
        budget = self.install_budget()
        intervals, _, enabled = self.configure()
        self.assertTrue(enabled)
        self.assertEqual(intervals[XAU], .2)
        self.assertEqual(budget.snapshot()["capacity_reserve"], 301)
        self.assertEqual(budget.snapshot()["execution_limit"], 1199)
        budget.reserve(17)
        budget.observe({"X-MBX-USED-WEIGHT-1M": "300"})
        spent = budget.snapshot()
        self.save_runtime("1000", "1000")
        intervals, brackets, enabled = self.configure()
        released = budget.snapshot()
        self.assertTrue(enabled)
        self.assertEqual(intervals[XAU], 2)
        self.assertEqual(60 / intervals[XAU] + 60 / brackets, 31)
        self.assertEqual(released["capacity_reserve"], 31)
        self.assertEqual(released["execution_limit"], 1469)
        for key in ("used", "local_used", "aster_ip_used"):
            self.assertEqual(released[key], spent[key])
        self.save_runtime("999", "1000")
        self.configure()
        self.assertEqual(budget.snapshot()["capacity_reserve"], 301)
        self.assertEqual(budget.snapshot()["used"], spent["used"])

    def test_queued_fast_worker_rechecks_target_and_does_not_renew_cache(self):
        self.configure()
        ticks = [100.0]
        with patch("trading.engine.time.monotonic", side_effect=lambda: ticks[0]), \
             patch.object(self.f.market, "capacities", return_value={7: dec(1000000)}) as read:
            self.engine.poll_market(XAU)
            before = deepcopy(self.engine.markets[XAU])
            self.save_runtime("1000", "1000")
            ticks[0] = 100.201
            self.wall += .201
            self.assertEqual(self.engine.poll_market(XAU), 2)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(self.engine.markets[XAU], before)
            ticks[0] = 102.001
            self.wall += 1.8
            self.engine.poll_market(XAU)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(read.call_args.args[1], {5, 10, 20})
            self.assertEqual(self.engine.markets[XAU]["fast_leverages"], [])
            self.save_runtime("999", "1000")
            ticks[0] = 102.202
            self.engine.poll_market(XAU)
            self.assertEqual(read.call_count, 3)
            self.assertEqual(read.call_args.args[1], {7})

    def test_queued_worker_keeps_shared_fast_consumer_after_completion(self):
        self.f.store.save_account(account("independent"))
        self.configure()
        ticks = [100.0]
        with patch("trading.engine.time.monotonic", side_effect=lambda: ticks[0]), \
             patch.object(self.f.market, "capacities", return_value={5: dec(1000000)}) as read:
            self.engine.poll_market(XAU)
            self.save_runtime("1000", "1000")
            ticks[0] = 100.201
            self.engine.poll_market(XAU)
            self.assertEqual(read.call_count, 2)
            self.assertEqual(read.call_args.args[1], {5})
            self.assertEqual(self.engine.markets[XAU]["fast_leverages"], [5])


class PairDailyCapacitySchedulerTests(unittest.TestCase):
    run_sampling = cadence.PairCapacitySchedulerTests.run_sampling

    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.h = _Harness(self.f)
        self.addCleanup(self.h.engine.dashboard_reports.close)
        self.pair = save_cycle_pair(self.f.store)
        self.f.store.put("pair_runtime:gold", runtime())
        self.h.api.budget = RateBudget()

    def test_real_scheduler_slows_on_completion_and_restores_after_limit_increase(self):
        h, starts, reserves, pair_calls = self.h, [], [], []

        def poll(symbol):
            starts.append((symbol, h.ticks))
            return h.engine.capacity_interval(symbol)

        def control(step):
            reserves.append(h.api.budget.snapshot()["capacity_reserve"])
            pair_calls.append(h.engine.pairs.tick.call_count)
            if step == 1:
                self.f.store.put("pair_runtime:gold", runtime("1000", "1000"))
                h.ticks = 100.3
            elif step == 2:
                h.ticks = 101.999
            elif step == 3:
                h.ticks = 102.001
            elif step == 4:
                self.pair["cycle"]["daily_volume_limit"] = "2000"
                self.f.store.save_pair(self.pair)
                h.engine.accounts_generation += 1
                h.ticks = 102.202
            else:
                h.engine.shutdown.set()

        brackets, books = self.run_sampling(poll, control)
        self.assertEqual(starts, [(XAU, 100), (XAU, 102.001), (XAU, 102.202)])
        self.assertEqual(reserves, [301, 31, 31, 31, 301])
        self.assertGreater(pair_calls[2], pair_calls[1])
        self.assertEqual({call.args[0] for call in brackets.call_args_list}, {XAU})
        self.assertEqual({call.args[0] for call in books.call_args_list}, set(SYMBOLS))

    def test_completion_and_resume_preserve_failed_request_backoff(self):
        h, starts = self.h, []

        def poll(symbol):
            starts.append(h.ticks)
            return PollBackoff(10) if len(starts) == 1 else h.engine.capacity_interval(symbol)

        def control(step):
            if step == 1:
                self.f.store.put("pair_runtime:gold", runtime("1000", "1000"))
                h.ticks = 100.5
            elif step == 2:
                self.pair["cycle"]["daily_volume_limit"] = "2000"
                self.f.store.save_pair(self.pair)
                h.engine.accounts_generation += 1
                h.ticks = 101
            elif step == 3:
                h.ticks = 110.499
                self.f.store.put("pair_runtime:gold", runtime("1000", "1000", now=h.wall))
            elif step == 4:
                h.ticks = 110.501
            else:
                h.engine.shutdown.set()

        self.run_sampling(poll, control)
        self.assertEqual(starts, [100, 110.501])


if __name__ == "__main__":
    unittest.main()
