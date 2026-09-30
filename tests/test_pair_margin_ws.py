"""Use real broker/private-stream evidence around simulated transfer HTTP."""
import copy
import json
import math
import time
import unittest
from unittest.mock import patch

from tests import test_pair_margin as fixtures
from trading import margin_balance
from trading.exchange import AmbiguousOrder, LiveBroker
from trading.margin_balance import MarginBalancer
from trading.models import TradingError, dec


class PairMarginWebSocketTests(unittest.TestCase):
    # Reuse setup helpers without importing/inheriting the original TestCase:
    # unittest must not discover and run its 52 unrelated tests twice.
    setUp = fixtures.PairMarginTests.setUp
    state = fixtures.PairMarginTests.state
    ready = fixtures.PairMarginTests.ready

    def connect(self, broker):
        with patch("trading.exchange.PrivateAccountStream.start"):
            broker.start_cycle_hot_data(["XAUUSD1"])
        broker.cycle_stream._set_connected(True)
        self.addCleanup(broker.stop_cycle_hot_data)

    def live(self, *, connect=True, wallet=True):
        snapshots = fixtures.PairMarginTests.live(self)
        for side, broker in self.brokers.items():
            if wallet:
                broker.api.account["assets"][0]["walletBalance"] = str(snapshots[side].wallet)
            if connect:
                self.connect(broker)
            snapshots[side].account_read_generation = broker._snapshot_generation
        self.after_http = lambda pending: None
        self.submitted = None
        original = margin_balance.TransferAPI
        owner = self

        class Transfer(original):
            def call(self, *args, **kwargs):
                try:
                    return super().call(*args, **kwargs)
                finally:
                    owner.submitted = copy.deepcopy(owner.state()["pending"])
                    owner.after_http(owner.submitted)

        patcher = patch("trading.margin_balance.TransferAPI", Transfer)
        patcher.start()
        self.addCleanup(patcher.stop)
        return snapshots

    def emit(self, pending, *, sides=("long", "short"), change=None):
        for side in sides:
            sign = -1 if side == pending["source"] else 1
            amount = dec(pending["amount"]) * sign
            base = dec(pending["ws_checkpoints"][side]["wallet"])
            stamp = max(math.ceil(pending["created_at"] * 1000), int(time.time() * 1000))
            event = {"e": "ACCOUNT_UPDATE", "E": stamp, "T": stamp,
                "a": {"m": "ASSET_TRANSFER", "B": [{"a": "USD1", "bc": str(amount), "wb": str(base + amount)}], "P": []}}
            if change:
                change(side, event)
            self.brokers[side].cycle_stream._handle_message(json.dumps(event))

    def expire_grace(self, snapshots):
        pending = self.state()["pending"]
        self.ready()
        with patch("time.time", return_value=pending["acknowledged_at"] + 2.01):
            return self.balancer.tick(self.pair, snapshots)

    def assert_rest(self, result):
        self.assertIsNone(result["pending"], result)
        self.assertEqual(result["last_transfer"]["refresh_source"], "rest")
        self.assertEqual(self.refreshed, ["long", "short"])
        self.assertEqual(len(self.transfers), 1)
        self.assertTrue(result["blocks_trading"])

    def test_both_transfer_events_before_ack_skip_both_rest_refreshes(self):
        snapshots = self.live()
        self.after_http = self.emit
        result = self.balancer.tick(self.pair, snapshots)
        self.assertIsNone(result["pending"], result)
        self.assertEqual(result["last_transfer"]["refresh_source"], "websocket")
        self.assertEqual(self.refreshed, [])
        self.assertEqual(len(self.transfers), 1)
        self.assertTrue(result["blocks_trading"])
        self.assertEqual(self.submitted["status"], "submitting")
        self.assertEqual(set(self.submitted["ws_checkpoints"]), {"long", "short"})
        audit = self.state()["last_transfer"]
        self.assertEqual(set(audit["balance_evidence"]), {"long", "short"})
        self.assertEqual(audit["balance_evidence"]["long"]["delta"], "-1000.00000000")
        self.assertNotIn("stream_session", str(audit["balance_evidence"]))
        self.assertNotIn("stream_session", str(result))
        self.assertNotIn("ws_checkpoints", str(MarginBalancer.status_view(self.pair, self.state())))
        for side, broker in self.brokers.items():
            with self.assertRaises(TradingError):
                broker.require_snapshot_current(snapshots[side])
            self.assertIsNone(broker.leverage_snapshot)
        self.assertEqual(self.balancer.tick(self.pair, snapshots)["status"], "cooldown")
        self.assertEqual(len(self.transfers), 1)

    def test_event_after_ack_completes_within_nonblocking_two_second_grace(self):
        snapshots = self.live()
        with patch("trading.margin_balance.time.sleep", side_effect=AssertionError("must not block")):
            first = self.balancer.tick(self.pair, snapshots)
        pending = self.state()["pending"]
        self.assertIsNotNone(first["pending"])
        self.assertLessEqual(first["next_check_at"], pending["acknowledged_at"] + 2)
        self.assertEqual(self.refreshed, [])
        self.emit(pending)
        self.ready()
        final = self.balancer.tick(self.pair, snapshots)
        self.assertIsNone(final["pending"], final)
        self.assertEqual(final["last_transfer"]["refresh_source"], "websocket")
        self.assertEqual(self.refreshed, [])
        self.assertEqual(len(self.transfers), 1)
        self.assertTrue(final["blocks_trading"])

    def test_absent_events_fall_back_once_after_fixed_deadline(self):
        snapshots = self.live()
        self.balancer.tick(self.pair, snapshots)
        pending = self.state()["pending"]
        self.ready()
        with patch("time.time", return_value=pending["acknowledged_at"] + 1.5):
            waiting = self.balancer.tick(self.pair, snapshots)
        self.assertIsNotNone(waiting["pending"])
        self.assertEqual(waiting["next_check_at"], pending["acknowledged_at"] + 2)
        self.assert_rest(self.expire_grace(snapshots))

    def test_offline_or_missing_wallet_baseline_uses_rest_immediately(self):
        for options in ({"connect": False}, {"wallet": False}):
            with self.subTest(options=options):
                self.store.put("pair_margin:gold", {})
                snapshots = self.live(**options)
                result = self.balancer.tick(self.pair, snapshots)
                self.assertNotIn("ws_checkpoints", self.submitted)
                self.assert_rest(result)

    def test_checkpoint_uses_latest_total_wallet_without_substituting_cross_or_available(self):
        snapshots = self.live()
        for side, value in (("long", "9000"), ("short", "7000")):
            self.brokers[side].api.account["assets"][0]["walletBalance"] = value
        self.after_http = self.emit
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(self.submitted["ws_checkpoints"]["long"]["wallet"], "9000")
        self.assertEqual(self.submitted["ws_checkpoints"]["short"]["wallet"], "7000")
        self.assertEqual(result["last_transfer"]["refresh_source"], "websocket")
        self.assertEqual(self.refreshed, [])

    def test_invalid_wallet_baseline_does_not_authorize_ws_or_block_rest(self):
        snapshots = self.live()
        self.brokers["long"].api.account["assets"][0]["walletBalance"] = "NaN"
        result = self.balancer.tick(self.pair, snapshots)
        self.assertNotIn("ws_checkpoints", self.submitted)
        self.assert_rest(result)

    def test_mismatched_or_one_sided_events_cannot_refresh(self):
        cases = {
            "amount": lambda side, event: event["a"]["B"][0].update(bc="1"),
            "wallet": lambda side, event: event["a"]["B"][0].update(wb="9"),
            "asset": lambda side, event: event["a"]["B"][0].update(a="USDT"),
            "reason": lambda side, event: event["a"].update(m="FUNDING_FEE"),
            "old_source_time": lambda side, event: event.update(T=int(self.submitted["created_at"] * 1000) - 1),
        }
        for name in (*cases, "one_side"):
            with self.subTest(name=name):
                self.store.put("pair_margin:gold", {})
                snapshots = self.live()
                self.after_http = lambda pending: self.emit(pending,
                    sides=("long",) if name == "one_side" else ("long", "short"), change=cases.get(name))
                first = self.balancer.tick(self.pair, snapshots)
                self.assertIsNotNone(first["pending"], first)
                self.assertEqual(self.refreshed, [])
                self.assert_rest(self.expire_grace(snapshots))

    def test_request_before_checkpoint_event_is_not_reused(self):
        snapshots = self.live()
        existing = {"source": "long", "amount": "1000", "created_at": time.time(), "ws_checkpoints":
            {side: broker.transfer_ws_checkpoint(snapshots[side].wallet) for side, broker in self.brokers.items()}}
        self.emit(existing)
        for side, broker in self.brokers.items():
            snapshots[side].account_read_generation = broker._snapshot_generation
        first = self.balancer.tick(self.pair, snapshots)
        self.assertIsNotNone(first["pending"])
        self.assert_rest(self.expire_grace(snapshots))

    def test_disconnect_after_matching_events_immediately_uses_rest(self):
        snapshots = self.live()
        def disconnect(pending):
            self.emit(pending)
            self.brokers["short"].cycle_stream._set_connected(False)
        self.after_http = disconnect
        self.assert_rest(self.balancer.tick(self.pair, snapshots))

    def test_expired_events_and_restarted_brokers_require_rest(self):
        for mode in ("expired", "restart"):
            with self.subTest(mode=mode):
                self.store.put("pair_margin:gold", {})
                snapshots = self.live()
                self.balancer.tick(self.pair, snapshots)
                pending = self.state()["pending"]
                self.emit(pending)
                if mode == "restart":
                    for side, previous in list(self.brokers.items()):
                        previous.stop_cycle_hot_data()
                        broker = LiveBroker({}, self.market, api=previous.api)
                        broker.snapshot = previous.snapshot
                        broker.margin_snapshot = previous.margin_snapshot
                        self.brokers[side] = broker
                        self.connect(broker)
                self.ready()
                delay = 31 if mode == "expired" else 0
                with patch("time.time", return_value=pending["acknowledged_at"] + delay):
                    self.assert_rest(MarginBalancer(self.engine).tick(self.pair, snapshots))

    def test_unknown_even_with_matching_events_never_guesses_or_resends(self):
        snapshots = self.live()
        self.response = AmbiguousOrder("simulated network timeout")
        self.after_http = self.emit
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "unknown")
        self.ready()
        with patch.object(self.balancer, "_refresh_acknowledged_ws", side_effect=AssertionError("unknown must use income")):
            retry = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(retry["status"], "unknown")
        self.assertIsNotNone(retry["pending"])
        self.assertTrue(retry["blocks_trading"])
        self.assertEqual(self.refreshed, [])
        self.assertEqual(len(self.transfers), 1)

    def test_legacy_zero_success_code_keeps_ack_and_original_rest_refresh(self):
        snapshots = self.live()
        self.response = {"code": 0, "msg": "success"}
        self.after_http = self.emit
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "acknowledged")
        self.assert_rest(result)

    def test_legacy_accepted_and_submitting_still_require_income_reconciliation(self):
        for status in ("accepted", "submitting"):
            with self.subTest(status=status):
                self.store.put("pair_margin:gold", {})
                snapshots = self.live()
                self.balancer.tick(self.pair, snapshots)
                journal = self.state()
                journal["pending"]["status"] = status
                journal["next_check_at"] = 0
                self.store.put("pair_margin:gold", journal)
                self.emit(journal["pending"])
                with patch.object(self.balancer, "_refresh_acknowledged_ws", side_effect=AssertionError("legacy must use income")):
                    result = self.balancer.tick(self.pair, snapshots)
                self.assertEqual(result["status"], "unknown" if status == "submitting" else status)
                self.assertIsNotNone(result["pending"])
                self.assertEqual(self.refreshed, [])
                self.assertEqual(len(self.transfers), 1)

    def test_new_event_revokes_balance_guards_before_journal_commit(self):
        snapshots = self.live()
        self.after_http = self.emit
        original = self.brokers["short"].transfer_ws_balance
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.brokers["long"]._cycle_account_event("ACCOUNT_CONFIG_UPDATE")
            return result
        with patch.object(self.brokers["short"], "transfer_ws_balance", side_effect=changed):
            first = self.balancer.tick(self.pair, snapshots)
        self.assertIsNotNone(first["pending"])
        self.assertEqual(self.refreshed, [])
        self.assert_rest(self.expire_grace(snapshots))


if __name__ == "__main__":
    unittest.main()
