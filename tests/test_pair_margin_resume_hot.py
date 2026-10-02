"""Resume cycle account maintenance after an unknown transfer balance refresh."""
from contextlib import ExitStack
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_margin_resume_execution as fixtures
from tests.helpers import seed_cycle_capacity
from trading.account_cache import HotAccountUnavailable
from trading.engine import CYCLE_HOT_POLL_INTERVAL
from trading.models import dec


SYMBOL = "XAUUSD1"
ORDER = "/fapi/v3/order"


class PairMarginResumeHotTests(TestCase):
    live_brokers = fixtures.PairMarginResumeExecutionTests.live_brokers
    pending = fixtures.PairMarginResumeExecutionTests.pending
    calls = fixtures.PairMarginResumeExecutionTests.calls
    tick = fixtures.PairMarginResumeExecutionTests.tick
    unknown = fixtures.PairMarginResumeExecutionTests.unknown
    posts = fixtures.PairMarginResumeExecutionTests.posts
    publish = fixtures.PairMarginResumeExecutionTests.publish

    def setUp(self):
        fixtures.PairMarginResumeExecutionTests.setUp(self)
        self.pair["ordinary"]["enabled"] = False
        self.pair["cycle"]["enabled"] = True
        self.pair = self.store.save_pair(self.pair)
        # Construct and connect the real private-stream/cache plumbing, but
        # prevent the stream's network worker from ever starting.
        stream = patch("trading.exchange.PrivateAccountStream.start")
        stream.start()
        self.addCleanup(stream.stop)
        for side, broker in self.brokers.items():
            broker.start_cycle_hot_data([SYMBOL])
            broker.cycle_stream._set_connected(True)
            self.addCleanup(broker.stop_cycle_hot_data)
            self.publish(side)

    def test_resumed_unknown_refreshes_revoked_hot_snapshots_and_really_opens_cycle(self):
        original = self.unknown()
        old_stamps = {side: broker.cycle_hot_snapshot([SYMBOL]).snapshot.timestamp
                      for side, broker in self.brokers.items()}
        self.wall += 2
        self.ticks += 2
        for side in self.brokers:
            self.engine.revoke_cycle_hot_data(side, "配对组状态或配置已更新")
        with ExitStack() as stack:
            ready = stack.enter_context(patch.object(self.engine, "cycle_hot_ready", wraps=self.engine.cycle_hot_ready))
            transfer = stack.enter_context(patch("trading.margin_balance.TransferAPI",
                side_effect=AssertionError("an unknown transfer must not be repeated")))
            for side, broker in self.brokers.items():
                with self.assertRaises(HotAccountUnavailable):
                    broker.cycle_hot_snapshot([SYMBOL])
                with patch.object(broker, "refresh_cycle_hot_snapshot", wraps=broker.refresh_cycle_hot_snapshot) as refresh:
                    self.assertEqual(self.engine.poll_cycle_hot_data(side), CYCLE_HOT_POLL_INTERVAL)
                    self.assertEqual(refresh.call_count, 1)
                lease = broker.cycle_hot_snapshot([SYMBOL])
                self.assertEqual(lease.snapshot.timestamp, self.wall)
                self.assertGreater(lease.snapshot.timestamp, old_stamps[side])
            self.assertCountEqual([call.args[0] for call in ready.call_args_list], ["long", "short"])
            seed_cycle_capacity(self.engine)
            state = self.tick()
            transfer.assert_not_called()
        self.assertEqual(state["phase"], "holding", state)
        self.assertIsNone(state["pending"])
        self.assertTrue(all(dec(quantity) > 0 for quantity in state["progress"]["quantities"].values()))
        self.assertEqual(len(self.posts()), 2)
        self.assertTrue(all(path == ORDER for _, path, _ in self.posts()))
        for side, broker in self.paper.items():
            self.assertEqual(len(broker.state["orders"]), 1)
            self.assertEqual(next(iter(broker.state["orders"].values()))["status"], "FILLED")
            self.assertGreater(state["snapshots"][side]["timestamp"], old_stamps[side])
        self.assertEqual(self.store.get("pair_margin:gold")["pending"], original)

    def test_unresolved_transfers_and_order_intents_still_block_hot_refresh(self):
        original = self.unknown()
        cases = [("without_baseline", {key: value for key, value in original.items() if key != "trading_baseline_at"}, {})]
        cases += [(status, {**original, "status": status}, {})
                  for status in ("accepted", "acknowledged", "submitting")]
        cases += [("invalid_marker", {**original, "trading_baseline_at": True}, {}),
                  ("future_marker", {**original, "trading_baseline_at": self.wall + 10}, {}),
                  ("order_pending", original, {"pending": {"id": "inflight-order"}})]
        for label, pending, runtime in cases:
            with self.subTest(blocker=label):
                self.store.put("pair_margin:gold", {"pending": deepcopy(pending)})
                self.store.put("pair_runtime:gold", runtime)
                for side, broker in self.brokers.items():
                    with patch.object(broker, "refresh_cycle_hot_snapshot", wraps=broker.refresh_cycle_hot_snapshot) as refresh:
                        self.assertEqual(self.engine.poll_cycle_hot_data(side), CYCLE_HOT_POLL_INTERVAL)
                        refresh.assert_not_called()
        self.assertEqual(self.posts(), [])

    def test_resumed_hot_refresh_does_not_remove_insufficient_funds_reservation(self):
        # The cycle supports small orders: leave only 1 USD1, below the
        # collateral required for the symbol's minimum quantity after reserve.
        original = self.unknown("24999")
        self.wall += 1
        self.ticks += 1
        for side, broker in self.brokers.items():
            self.engine.revoke_cycle_hot_data(side, "new reserved trading baseline")
            with patch.object(broker, "refresh_cycle_hot_snapshot", wraps=broker.refresh_cycle_hot_snapshot) as refresh:
                self.assertEqual(self.engine.poll_cycle_hot_data(side), CYCLE_HOT_POLL_INTERVAL)
                refresh.assert_called_once()
            self.assertEqual(broker.cycle_hot_snapshot([SYMBOL]).snapshot.timestamp, self.wall)
        seed_cycle_capacity(self.engine)
        with patch("trading.margin_balance.TransferAPI",
                side_effect=AssertionError("an unknown transfer must not be repeated")) as transfer:
            state = self.tick()
            transfer.assert_not_called()
        self.assertFalse(state["margin"]["blocks_trading"], state)
        self.assertEqual(state["phase"], "waiting", state)
        self.assertIsNone(state["pending"])
        self.assertEqual(state["progress"]["quantities"], {"LONG": "0", "SHORT": "0"})
        self.assertEqual(self.posts(), [])
        self.assertTrue(all(not broker.state["orders"] for broker in self.paper.values()))
        self.assertEqual(self.store.get("pair_margin:gold")["pending"], original)

    def test_pair_revision_change_during_resumed_refresh_revokes_new_lease(self):
        self.unknown()
        broker = self.brokers["long"]
        refresh = broker.refresh_cycle_hot_snapshot
        leases = []

        def refresh_then_reconfigure():
            result = refresh()
            leases.append(broker.cycle_hot_snapshot([SYMBOL]))
            pair = self.store.pair("gold")
            self.store.save_pair({**pair, "name": "changed during refresh"})
            return result

        with patch.object(broker, "refresh_cycle_hot_snapshot", side_effect=refresh_then_reconfigure), \
                patch.object(self.engine, "cycle_hot_ready", wraps=self.engine.cycle_hot_ready) as ready:
            self.assertEqual(self.engine.poll_cycle_hot_data("long"), CYCLE_HOT_POLL_INTERVAL)
            ready.assert_not_called()
        self.assertEqual(len(leases), 1)
        with self.assertRaises(HotAccountUnavailable):
            leases[0].require_fresh()
        self.assertEqual(self.posts(), [])

    def test_paused_pending_transfer_retains_private_stream_without_rest_refresh(self):
        self.unknown()
        self.engine.pairs.enable("gold", False)
        for side, broker in self.brokers.items():
            private_stream = broker.cycle_stream
            with patch.object(broker, "start_cycle_hot_data", wraps=broker.start_cycle_hot_data) as start, \
                    patch.object(broker, "stop_cycle_hot_data", wraps=broker.stop_cycle_hot_data) as stop, \
                    patch.object(broker, "refresh_cycle_hot_snapshot", wraps=broker.refresh_cycle_hot_snapshot) as refresh:
                self.assertEqual(self.engine.poll_cycle_hot_data(side), 30)
                start.assert_called_once()
                stop.assert_not_called()
                refresh.assert_not_called()
            self.assertIs(broker.cycle_stream, private_stream)
        self.assertEqual(self.posts(), [])

    def test_new_baseline_wakes_accounts_without_erasing_existing_hot_backoff(self):
        self.unknown(ready=False)
        for side in self.brokers:
            # Attach the engine's real cache listener while recovery still owns
            # the group. This first poll must not make account REST requests.
            self.assertEqual(self.engine.poll_cycle_hot_data(side), CYCLE_HOT_POLL_INTERVAL)
            work = self.engine.work(side)
            work.hot_wake = False
            work.hot_backoff = self.ticks + 60
        deadlines = {side: self.engine.work(side).hot_backoff for side in self.brokers}
        journal = self.store.get("pair_margin:gold")
        journal["next_check_at"] = 0
        self.store.put("pair_margin:gold", journal)
        self.tick()  # Real balance reads establish the baseline and revoke hot data.
        resumed = self.store.get("pair_margin:gold")["pending"]
        self.assertEqual(resumed["trading_baseline_at"], self.wall)
        self.assertEqual(self.posts(), [])
        for side in self.brokers:
            self.assertTrue(self.engine.work(side).hot_wake)
            self.assertEqual(self.engine.work(side).hot_backoff, deadlines[side])
            self.engine.wake_cycle_hot_data(side)
            self.assertEqual(self.engine.work(side).hot_backoff, deadlines[side])
        # Model the scheduler reaching those existing deadlines; the next real
        # maintenance pass must now be allowed to rebuild both hot snapshots.
        self.wall += 60
        self.ticks += 60
        for side, broker in self.brokers.items():
            with patch.object(broker, "refresh_cycle_hot_snapshot", wraps=broker.refresh_cycle_hot_snapshot) as refresh:
                self.assertEqual(self.engine.poll_cycle_hot_data(side), CYCLE_HOT_POLL_INTERVAL)
                refresh.assert_called_once()
            broker.cycle_hot_snapshot([SYMBOL]).require_fresh()
        self.assertEqual(self.store.get("pair_margin:gold")["pending"], resumed)
        self.assertEqual(self.posts(), [])
