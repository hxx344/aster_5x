"""Shared depth seeding and request admission using only offline transports."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import threading
import unittest
from unittest.mock import Mock, patch

import httpx

from trading.depth import DEPTH_RESYNC_INTERVAL
from trading.depth_stream import PublicDepthStream
from trading.engine import Engine
from trading.exchange import API, BudgetWait, ExchangeError, LiveBroker, MarketData, RateBudget
from trading.migration import DEFAULT_MIGRATION
from trading.models import SYMBOLS, TradingError
from tests.helpers import Fixture


SYMBOL = "CLUSD1"


class WSDepthIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.wall, self.ticks = 100.0, 100.0
        wall = patch("trading.exchange.time.time", side_effect=lambda: self.wall)
        ticks = patch("trading.exchange.time.monotonic", side_effect=lambda: self.ticks)
        wall.start()
        ticks.start()
        self.addCleanup(wall.stop)
        self.addCleanup(ticks.stop)
        self.raw = {"E": 100000, "T": 100000, "lastUpdateId": 12,
                    "bids": [["99", "1000"], ["98", "1000"]],
                    "asks": [["100", "1000"], ["101", "1000"]]}
        self.calls = []

        def respond(request):
            self.calls.append(request.url.path)
            return httpx.Response(200, json=deepcopy(self.raw))

        self.api = API(transport=httpx.MockTransport(respond), budget=RateBudget(capacity_reserve=180))
        self.addCleanup(self.api.close)
        self.depth_stream = PublicDepthStream(clock=lambda: self.wall, monotonic=lambda: self.ticks)
        # Exercise the real parser/cache without starting a network receiver.
        self.depth_stream._connected = True
        self.depth_stream._connection = 1
        self.addCleanup(self.depth_stream.close)
        self.quotes = Mock(spec=["book", "start", "close"])
        self.market = MarketData(self.api, stream=self.quotes, depth_stream=self.depth_stream)
        self.event()

    def event(self, *, symbol=SYMBOL, first=10, last=12, previous=9, bids=None):
        self.depth_stream._handle_message(json.dumps({
            "stream": symbol.lower() + "@depth@100ms",
            "data": {"e": "depthUpdate", "s": symbol, "E": int(self.wall * 1000),
                     "T": int(self.wall * 1000), "U": first, "u": last, "pu": previous,
                     "b": bids or [], "a": []}}))

    def test_initial_rest_seed_then_display_and_execution_share_free_updates(self):
        self.assertEqual(self.market.depth_weight([SYMBOL]), 20)
        first = self.market.depth(SYMBOL)
        self.assertEqual(first.display()["spreads"]["50000"]["status"], "ok")
        for update in range(13, 63):
            self.wall += .1
            self.ticks += .1
            self.event(first=update, last=update, previous=update - 1, bids=[["99", str(update)]])
            display = self.market.depth(SYMBOL)
            execution = self.market.depth(SYMBOL)
            self.assertEqual(display.bids, execution.bids)
            self.assertEqual(execution.bids[0][1], update)
        self.assertEqual(self.calls, ["/fapi/v3/depth"])
        self.assertEqual(self.api.budget.weight, 20)
        self.assertEqual(self.market.depth_weight([SYMBOL]), 0)
        self.quotes.book.assert_not_called()

    def test_exhausted_rest_budget_does_not_block_healthy_shared_depth(self):
        self.market.depth(SYMBOL)
        self.api.budget.reserve(1300)
        self.market.depth(SYMBOL).require_fresh()
        self.assertEqual(self.api.budget.weight, 1320)
        self.assertEqual(len(self.calls), 1)

    def test_concurrent_readers_share_one_seed_request(self):
        entered, release = threading.Event(), threading.Event()
        call = self.api.call

        def blocked(*args, **kwargs):
            entered.set()
            if not release.wait(2):
                raise AssertionError("seed was not released")
            return call(*args, **kwargs)

        with patch.object(self.api, "call", side_effect=blocked), ThreadPoolExecutor(max_workers=6) as pool:
            futures = [pool.submit(self.market.depth, SYMBOL) for _ in range(6)]
            try:
                self.assertTrue(entered.wait(2))
            finally:
                release.set()
            snapshots = [future.result(2) for future in futures]
        self.assertTrue(all(row.bids == snapshots[0].bids for row in snapshots))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.api.budget.weight, 20)

    def test_gap_revokes_old_snapshot_and_reseed_is_cooled_down(self):
        old = self.market.depth(SYMBOL)
        self.event(first=18, last=18, previous=17)
        with self.assertRaises(TradingError):
            old.require_fresh()
        self.event(first=19, last=19, previous=18)
        with self.assertRaises(ExchangeError) as waiting:
            self.market.depth(SYMBOL)
        self.assertEqual(waiting.exception.retry_after, DEPTH_RESYNC_INTERVAL)
        self.assertEqual(len(self.calls), 1)
        self.wall += DEPTH_RESYNC_INTERVAL
        self.ticks += DEPTH_RESYNC_INTERVAL
        self.raw.update(E=int(self.wall * 1000), T=int(self.wall * 1000), lastUpdateId=20)
        self.event(first=20, last=20, previous=19)
        self.market.depth(SYMBOL).require_fresh()
        self.assertEqual(len(self.calls), 2)
        with self.assertRaises(TradingError):
            old.require_fresh()

    def test_disconnect_and_quiet_expiry_do_not_fall_back_to_periodic_rest(self):
        self.market.depth(SYMBOL)
        self.wall += 16
        self.ticks += 16
        for _ in range(5):
            with self.assertRaises(TradingError):
                self.market.depth(SYMBOL)
        self.assertEqual(len(self.calls), 1)
        self.depth_stream.close()
        for _ in range(5):
            with self.assertRaises(TradingError):
                self.market.depth(SYMBOL)
        self.assertEqual(len(self.calls), 1)

    def test_rejected_seed_has_no_rest_snapshot_escape_and_honors_backoff(self):
        self.raw["lastUpdateId"] = True
        with self.assertRaises(TradingError):
            self.market.depth(SYMBOL)
        self.assertIsNone(self.depth_stream.snapshot(SYMBOL))
        self.event()
        with self.assertRaises(ExchangeError):
            self.market.depth(SYMBOL)
        self.assertEqual(len(self.calls), 1)


class MigrationDepthAdmissionTests(unittest.TestCase):
    def test_valid_plan_still_reserves_full_private_mode_recheck_and_submission(self):
        fixture = Fixture()
        self.addCleanup(fixture.close)
        account = fixture.store.account("test")
        account["migration"] = {**DEFAULT_MIGRATION, "enabled": True}
        fixture.store.save_account(account)
        for side in ("LONG", "SHORT"):
            fixture.broker.state["positions"]["XAUUSD1:" + side] = {"qty": "1", "entry": "4412.01"}
        engine = Engine(fixture.store, demo=True, market=fixture.market)
        for symbol in SYMBOLS:
            engine.poll_market(symbol)
        broker = Mock(spec=LiveBroker)
        broker.api = Mock()
        broker.api.budget = RateBudget(capacity_reserve=180)
        broker.api.budget.reserve(1250)
        broker.snapshot_weight.return_value = 79
        with self.assertRaises(BudgetWait):
            engine.tick_migration(account, fixture.broker.snapshot(SYMBOLS), broker)
        broker.snapshot_weight.assert_called_once_with(list(SYMBOLS), fresh_modes=True)
        broker.snapshot.assert_not_called()
        broker.submit.assert_not_called()
        self.assertIsNone(fixture.store.intent("test"))

    def test_healthy_depth_can_be_evaluated_without_sixty_phantom_weight(self):
        fixture = Fixture()
        self.addCleanup(fixture.close)
        account = fixture.store.account("test")
        account["migration"] = {**DEFAULT_MIGRATION, "enabled": True}
        fixture.store.save_account(account)
        for side in ("LONG", "SHORT"):
            fixture.broker.state["positions"]["XAUUSD1:" + side] = {"qty": "1", "entry": "4412.01"}
        engine = Engine(fixture.store, demo=True, market=fixture.market)
        for symbol in SYMBOLS:
            engine.poll_market(symbol)
        broker = Mock(spec=LiveBroker)
        broker.api = Mock()
        broker.api.budget = RateBudget(capacity_reserve=180)
        broker.api.budget.reserve(1300)
        with patch("trading.engine.plan_migration", side_effect=TradingError("no valid plan")) as plan:
            engine.tick_migration(account, fixture.broker.snapshot(SYMBOLS), broker)
        self.assertEqual(plan.call_count, 2)
        self.assertEqual(broker.api.budget.weight, 1300)
        broker.snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
