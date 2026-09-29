"""Public quota sampling follows real consumers and durable pair recovery."""
from contextlib import ExitStack
import unittest
from unittest.mock import patch

from tests.helpers import Fixture, account
from tests.test_cycle_ws_scheduler import _Harness, _Pool
from trading.engine import Engine
from trading.exchange import API, MarketData, RateBudget
from trading.models import SYMBOLS, dec
from trading.pairing import validate_pair
from trading.scheduling import PollBackoff


XAU, SPCX, CL = SYMBOLS


def save_ordinary_pair(store, pair_id="gold", long_id="test", short_id="second"):
    for aid in (long_id, short_id):
        store.save_account({**account(aid), "enabled": False})
    return store.save_pair(validate_pair({
        "id": pair_id, "name": pair_id, "long_account_id": long_id,
        "short_account_id": short_id, "enabled": True,
        "ordinary": {"enabled": True}, "cycle": {"enabled": False},
    }), create=True)


class PairCapacityCadenceTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.pair = save_ordinary_pair(self.f.store)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.addCleanup(self.engine.dashboard_reports.close)

    def targets(self):
        return self.engine.fast_capacity_targets(self.f.store.accounts())

    def pending(self, value):
        self.f.store.put("pair_runtime:gold", {"pending": value})

    def configure(self):
        engine = self.engine
        engine.capacity_targets = self.targets()
        engine.capacity_accounts = self.f.store.accounts()
        intervals, brackets, enabled = engine.capacity_poll_schedule(engine.capacity_targets)
        engine.capacity_intervals = intervals
        engine.capacity_brackets_interval = brackets
        engine.capacity_poll_enabled = enabled
        return intervals, brackets, enabled

    def install_budget(self):
        budget = RateBudget()
        api = API(budget=budget)
        self.addCleanup(api.close)
        self.engine.market = MarketData(api)
        return budget

    def test_default_monitoring_keeps_only_xau_quota_requests(self):
        self.assertEqual(self.engine.monitored_market_symbols(), set(SYMBOLS))
        self.assertEqual(self.engine.capacity_market_symbols(), {XAU})
        with patch.object(self.f.market, "capacities") as read:
            self.engine.poll_market(SPCX)
            self.engine.poll_market(CL)
        read.assert_not_called()

    def test_default_monitoring_skips_non_xau_public_brackets(self):
        self.install_budget()
        with patch.object(self.engine.market, "refresh_public_brackets") as read:
            self.engine.poll_public_brackets(SPCX)
            self.engine.poll_public_brackets(CL)
        read.assert_not_called()

    def test_independent_selected_market_survives_hidden_display(self):
        independent = account("independent")
        independent["policy"].update(symbols=[SPCX, CL], ordinary_symbol=CL)
        self.f.store.save_account(independent)
        self.f.store.edit_monitoring({"monitor": False}, symbol=CL)
        self.assertEqual(self.engine.capacity_market_symbols(), {XAU, CL})
        self.assertEqual(self.targets()[CL], {5})

    def test_migration_and_paused_pending_account_preserve_needed_markets(self):
        independent = account("independent")
        independent["migration"] = {"enabled": True}
        self.f.store.save_account(independent)
        self.assertEqual(self.engine.capacity_market_symbols(), set(SYMBOLS))
        independent.update(enabled=False, migration={"enabled": False})
        self.f.store.save_account(independent)
        self.f.store.save_intent({"id": "unfinished", "account_id": "independent",
                                "kind": "pair", "status": "pending", "symbol": SPCX})
        self.assertEqual(self.engine.capacity_market_symbols(), {XAU, SPCX})

    def test_pending_and_restarted_recovery_remove_only_pair_fast_demand(self):
        original = self.targets()
        self.assertEqual(original, {XAU: {5, 10, 20}})
        for kind in ("ordinary", "cycle", "leverage"):
            with self.subTest(kind=kind):
                self.pending({"id": "uncertain", "kind": kind, "phase": "open"})
                self.assertEqual(self.targets(), {})
                restarted = Engine(self.f.store, market=self.f.market)
                self.addCleanup(restarted.dashboard_reports.close)
                self.assertEqual(restarted.fast_capacity_targets(self.f.store.accounts()), {})
                self.assertEqual(restarted.capacity_market_symbols(), {XAU})
        self.pending(None)
        self.assertEqual(self.targets(), original)

    def test_paused_pair_does_not_rearm_fast_polling_when_pending_clears(self):
        pair = self.f.store.pair("gold")
        self.f.store.save_pair({**pair, "enabled": False})
        self.pending({"id": "uncertain"})
        self.assertEqual(self.targets(), {})
        self.assertEqual(self.engine.pairs.poll_interval(pair, {"pending": {"id": "uncertain"}}), 1)
        self.pending(None)
        self.assertEqual(self.targets(), {})

    def test_other_pair_and_independent_account_keep_shared_fast_feed(self):
        self.pending({"id": "uncertain"})
        independent = account("independent")
        self.f.store.save_account(independent)
        self.assertEqual(self.targets(), {XAU: {5}})
        self.f.store.save_account({**independent, "enabled": False})
        save_ordinary_pair(self.f.store, "other", "third", "fourth")
        self.assertEqual(self.targets(), {XAU: {5, 10, 20}})

    def test_only_selected_symbol_consumes_reserve_and_pending_releases_fast_reserve(self):
        budget = self.install_budget()
        intervals, brackets, enabled = self.configure()
        self.assertTrue(enabled)
        self.assertEqual(intervals[XAU], .2)
        self.assertEqual(budget.snapshot()["capacity_reserve"], 301)
        self.assertEqual(budget.snapshot()["execution_limit"], 1199)
        self.assertEqual(60 / intervals[XAU] + 60 / brackets, 301)
        self.pending({"id": "uncertain"})
        intervals, brackets, enabled = self.configure()
        self.assertTrue(enabled)
        self.assertEqual(intervals[XAU], 2)
        self.assertEqual(budget.snapshot()["capacity_reserve"], 31)
        self.assertEqual(budget.snapshot()["execution_limit"], 1469)
        self.pending(None)
        self.configure()
        self.assertEqual(budget.snapshot()["capacity_reserve"], 301)

    def test_low_and_zero_limits_never_overbook_selected_public_samples(self):
        budget = self.install_budget()
        for limit in (900, 120, 6, 1):
            with self.subTest(limit=limit):
                budget.limit = limit
                intervals, brackets, enabled = self.configure()
                reserve = budget.snapshot()["capacity_reserve"]
                if reserve:
                    self.assertTrue(enabled)
                    self.assertLessEqual(60 / intervals[XAU] + 60 / brackets, reserve + 1e-6)
                else:
                    self.assertFalse(enabled)
                    with patch.object(self.engine.market, "capacities") as read, \
                         patch.object(self.engine.market, "refresh_public_brackets") as bracket_read:
                        self.engine.poll_market(XAU)
                        self.engine.poll_public_brackets(XAU)
                    read.assert_not_called()
                    bracket_read.assert_not_called()

    def test_no_selected_symbols_releases_reserve_without_public_io(self):
        budget = self.install_budget()
        pair = self.f.store.pair("gold")
        self.f.store.save_pair({**pair, "enabled": False})
        self.f.store.edit_monitoring({"monitoring_enabled": False})
        self.assertEqual(self.engine.capacity_market_symbols(), set())
        _, _, enabled = self.configure()
        self.assertFalse(enabled)
        self.assertEqual(budget.snapshot()["capacity_reserve"], 0)
        with patch.object(self.engine.market, "capacities") as read:
            self.engine.poll_market(XAU)
        read.assert_not_called()

    def test_queued_fast_worker_obeys_new_pending_state_without_renewing_cache(self):
        self.configure()
        clock = [100.0]
        with patch("trading.engine.time.monotonic", side_effect=lambda: clock[0]), \
             patch("trading.engine.time.time", side_effect=lambda: 1_800_000_000 + clock[0]), \
             patch.object(self.f.market, "capacities", return_value={5: dec(1000000)}) as read:
            self.engine.poll_market(XAU)
            checked_at = self.engine.markets[XAU]["checked_at"]
            self.pending({"id": "uncertain"})
            clock[0] = 100.201
            self.engine.poll_market(XAU)
            self.assertEqual(read.call_count, 1)
            self.assertEqual(self.engine.markets[XAU]["checked_at"], checked_at)
            clock[0] = 102.001
            self.engine.poll_market(XAU)
            self.assertEqual(read.call_count, 2)
            self.pending(None)
            clock[0] = 102.202
            self.engine.poll_market(XAU)
            self.assertEqual(read.call_count, 3)

    def test_queued_worker_retains_fast_polling_for_another_consumer(self):
        self.f.store.save_account(account("independent"))
        self.configure()
        clock = [100.0]
        with patch("trading.engine.time.monotonic", side_effect=lambda: clock[0]), \
             patch("trading.engine.time.time", side_effect=lambda: 1_800_000_000 + clock[0]), \
             patch.object(self.f.market, "capacities", return_value={5: dec(1000000)}) as read:
            self.engine.poll_market(XAU)
            self.pending({"id": "uncertain"})
            clock[0] = 100.201
            self.engine.poll_market(XAU)
            self.assertEqual(read.call_count, 2)


class PairCapacitySchedulerTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.h = _Harness(self.f)
        self.addCleanup(self.h.engine.dashboard_reports.close)
        save_ordinary_pair(self.f.store)
        self.h.api.budget = RateBudget()

    def run_sampling(self, poll, control):
        h = self.h
        h.control = control
        with ExitStack() as stack:
            stack.enter_context(patch("trading.engine.ThreadPoolExecutor", return_value=_Pool()))
            stack.enter_context(patch("trading.engine.time.monotonic", side_effect=lambda: h.ticks))
            stack.enter_context(patch("trading.engine.time.time", side_effect=lambda: h.wall))
            stack.enter_context(patch.object(h.engine, "scheduling", return_value=h.timing))
            stack.enter_context(patch.object(h.engine, "tick_account", return_value=60))
            stack.enter_context(patch.object(h.engine.pairs, "tick", return_value=1))
            stack.enter_context(patch.object(h.engine, "poll_market", side_effect=poll))
            brackets = stack.enter_context(patch.object(h.engine, "poll_public_brackets", return_value=60))
            books = stack.enter_context(patch.object(h.engine, "poll_book", return_value=60))
            for name in ("poll_depth", "notify"):
                stack.enter_context(patch.object(h.engine, name, return_value=60))
            if h.engine.listing_monitor is not None:
                stack.enter_context(patch.object(h.engine.listing_monitor, "poll", return_value=60))
            h.engine.run()
        if h.failure:
            raise h.failure
        return brackets, books

    def test_pending_transition_slows_and_completion_restores_scheduler(self):
        h, starts, reserves = self.h, [], []

        def poll(symbol):
            starts.append((symbol, h.ticks))
            return h.engine.capacity_interval(symbol)

        def control(step):
            reserves.append(h.api.budget.snapshot()["capacity_reserve"])
            if step == 1:
                self.f.store.put("pair_runtime:gold", {"pending": {"id": "uncertain"}})
                h.ticks = 100.3
            elif step == 2:
                h.ticks = 101.999
            elif step == 3:
                h.ticks = 102.001
            elif step == 4:
                self.f.store.put("pair_runtime:gold", {"pending": None})
                h.ticks = 102.202
            else:
                h.engine.shutdown.set()

        brackets, books = self.run_sampling(poll, control)
        self.assertEqual(starts, [(XAU, 100), (XAU, 102.001), (XAU, 102.202)])
        self.assertEqual(reserves, [301, 31, 31, 31, 301])
        self.assertEqual({call.args[0] for call in brackets.call_args_list}, {XAU})
        self.assertEqual({call.args[0] for call in books.call_args_list}, set(SYMBOLS))

    def test_pause_resume_does_not_erase_failed_request_backoff(self):
        h, starts = self.h, []

        def poll(symbol):
            starts.append(h.ticks)
            return PollBackoff(10) if len(starts) == 1 else h.engine.capacity_interval(symbol)

        def control(step):
            if step == 1:
                self.f.store.put("pair_runtime:gold", {"pending": {"id": "uncertain"}})
                h.ticks = 100.5
            elif step == 2:
                self.f.store.put("pair_runtime:gold", {"pending": None})
                h.ticks = 101
            elif step == 3:
                # The scheduler first consumes the failed future at 100.5;
                # its full ten-second backoff starts at that observation.
                h.ticks = 110.499
            elif step == 4:
                h.ticks = 110.501
            else:
                h.engine.shutdown.set()

        self.run_sampling(poll, control)
        self.assertEqual(starts, [100, 110.501])


if __name__ == "__main__":
    unittest.main()
