"""A market pair can reuse revocable account leases through transfer admission."""
import time
from unittest import TestCase
from unittest.mock import patch

import httpx

from tests import test_pair_margin as fixtures
from trading.account_cache import HotAccountUnavailable
from trading.margin_balance import TransferAPI
from trading.pair_execution import PairTrader


class PairMarginHotLeaseTests(TestCase):
    live = fixtures.PairMarginTests.live
    state = fixtures.PairMarginTests.state

    def setUp(self):
        fixtures.PairMarginTests.setUp(self)
        snapshots = self.live()
        self.engine.market = self.market
        self.deadline = time.monotonic() + 4
        for side, broker in self.brokers.items():
            del snapshots[side].account_read_generation
            snapshots[side].open_orders = None
            broker.cycle_cache.configure(["XAUUSD1"])
            broker.cycle_cache.set_connected(True)
            self.assertTrue(broker.cycle_cache.publish(broker.cycle_cache.begin_refresh(), snapshots[side],
                time.monotonic(), valid_until_monotonic=self.deadline))
            original = broker.snapshot

            def after_transfer_only(*args, original=original, **kwargs):
                self.assertTrue(self.transfers, "duplicate pre-transfer snapshot read")
                return original(*args, **kwargs)

            broker.snapshot = after_transfer_only
        self.snapshots, self.guards = PairTrader(self.engine)._read(self.brokers, hot=True)

    def tick(self):
        return self.balancer.tick(self.pair, self.snapshots, snapshot_guards=self.guards)

    def test_hot_transfer_uses_80_weight_and_only_refreshes_after_acceptance(self):
        # 80 fits while even one redundant 72-weight snapshot would be rejected.
        from trading.exchange import RateBudget
        budget = RateBudget(capacity_reserve=363)
        budget.reserve(1057)
        for broker in self.brokers.values():
            broker.api.budget = budget
        result = self.tick()
        self.assertEqual(result["status"], "acknowledged", result)
        self.assertEqual(len(self.transfers), 1)
        self.assertEqual(self.refreshed, ["long", "short"])
        for broker in self.brokers.values():
            self.assertEqual([path for _, path, _ in broker.api.calls],
                ["/fapi/v3/accountWithJoinMargin", "/fapi/v3/income"])
        for guard in self.guards.values():
            with self.assertRaises(HotAccountUnavailable):
                guard()

    def test_revoked_lease_blocks_before_any_extra_account_requests(self):
        self.brokers["short"]._cycle_account_event("ACCOUNT_UPDATE")
        result = self.tick()
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(self.master_calls, [])
        self.assertEqual(self.refreshed, [])
        self.assertEqual(self.transfers, [])

    def test_mode_deadline_during_income_prevents_submission(self):
        original = self.brokers["short"].api.call
        with patch("time.monotonic", return_value=self.deadline - 1) as clock:
            def expire(method, path, *args, **kwargs):
                response = original(method, path, *args, **kwargs)
                if path.endswith("/income"):
                    clock.return_value = self.deadline
                return response

            with patch.object(self.brokers["short"].api, "call", side_effect=expire):
                result = self.tick()
        self.assertEqual(result["status"], "blocked", result)
        self.assertEqual(self.transfers, [])
        self.assertIsNone(self.state().get("pending"))

    def test_disconnect_after_intent_commit_is_definitively_not_sent(self):
        original = self.store.put

        def disconnect(key, value):
            original(key, value)
            if key == "pair_margin:gold" and (value.get("pending") or {}).get("status") == "submitting":
                self.brokers["short"].cycle_cache.set_connected(False)

        with patch.object(self.store, "put", side_effect=disconnect):
            result = self.tick()
        self.assertEqual(result["status"], "rejected", result)
        self.assertIsNone(self.state()["pending"])
        self.assertEqual(self.transfers, [])

    def test_live_http_transport_sees_both_leases_revoked(self):
        sent = []

        def transport(request):
            for guard in self.guards.values():
                with self.assertRaises(HotAccountUnavailable):
                    guard()
            sent.append(request.method)
            self.transfers.append(request.method)
            return httpx.Response(200, json={"code": 200, "msg": "success"})

        def factory(credentials, *, budget, before_submit):
            return TransferAPI(credentials, budget=budget, before_submit=before_submit,
                               transport=httpx.MockTransport(transport))

        with patch("trading.margin_balance.TransferAPI", side_effect=factory):
            result = self.tick()
        self.assertEqual(result["status"], "acknowledged", result)
        self.assertEqual(sent, ["POST"])

    def test_event_during_signing_still_prevents_http_transport(self):
        sent = []

        def factory(credentials, *, budget, before_submit):
            api = TransferAPI(credentials, budget=budget, before_submit=before_submit,
                transport=httpx.MockTransport(lambda request: sent.append(request) or
                                              httpx.Response(200, json={"code": 200, "msg": "success"})))
            original = api.signed_parameters

            def sign(params):
                result = original(params)
                self.brokers["long"]._cycle_account_event("ORDER_TRADE_UPDATE")
                return result

            api.signed_parameters = sign
            return api

        with patch("trading.margin_balance.TransferAPI", side_effect=factory):
            result = self.tick()
        self.assertEqual(result["status"], "rejected", result)
        self.assertEqual(sent, [])
        self.assertIsNone(self.state()["pending"])
