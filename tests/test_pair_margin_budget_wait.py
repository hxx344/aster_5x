"""Offline transfer budget admission, recovery priority, and durable waits."""
from copy import deepcopy
import time
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_margin as fixtures
from trading import margin_balance
from trading.exchange import AmbiguousOrder, BudgetWait, RateBudget
from trading.margin_balance import MarginBalancer


class PairMarginBudgetWaitTests(TestCase):
    live = fixtures.PairMarginTests.live
    state = fixtures.PairMarginTests.state
    ready = fixtures.PairMarginTests.ready

    def setUp(self):
        fixtures.PairMarginTests.setUp(self)
        self.wall, self.monotonic = time.time(), time.monotonic()
        for name, value in (("time.time", lambda: self.wall), ("time.monotonic", lambda: self.monotonic)):
            clock = patch(name, side_effect=value)
            clock.start()
            self.addCleanup(clock.stop)
        self.snapshots = self.live()
        self.budget = RateBudget(capacity_reserve=363)
        self.requests = []
        self.before_transfer = None
        owner = self
        for side, broker in self.brokers.items():
            broker.api.budget = self.budget
            original = broker.api.call

            def call(method, path, params=None, *, weight=1, side=side, original=original, **kwargs):
                owner.record(side, path, weight)
                return original(method, path, params, weight=weight, **kwargs)

            broker.api.call = call
            fresh = broker.snapshot

            def snapshot(symbols, fresh_modes=False, *, side=side, fresh=fresh):
                owner.record(side, "snapshot", 70)
                return fresh(symbols, fresh_modes=fresh_modes)

            broker.snapshot = snapshot

        master_class, transfer_class = margin_balance.API, margin_balance.TransferAPI

        class Master(master_class):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.budget = kwargs["budget"]

            def call(self, method, path, *args, weight=1, **kwargs):
                owner.record("master", path, weight)
                return super().call(method, path, *args, weight=weight, **kwargs)

        class Transfer(transfer_class):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.budget = kwargs["budget"]

            def call(self, method, path, params, *, weight=1, **kwargs):
                if owner.before_transfer:
                    owner.before_transfer()
                owner.record("master", path, weight)
                return super().call(method, path, params, weight=weight, **kwargs)

        for target, value in (("API", Master), ("TransferAPI", Transfer)):
            patcher = patch.object(margin_balance, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def record(self, side, path, weight):
        self.budget.reserve(weight)
        self.requests.append((side, path, weight, self.budget._priority_flags()))

    def advance(self, seconds):
        self.wall += seconds
        self.monotonic += seconds

    def tick(self):
        return self.balancer.tick(self.pair, self.snapshots)

    def pending(self, status="accepted", *, transaction=True):
        children = {side: self.creds["ASTER_" + side.upper()] for side in self.members}
        record = {"request_id": "offline-pending", "source": "long", "destination": "short",
            "amount": "10", "created_at": self.wall - 1, "status": status,
            "identity": MarginBalancer._fingerprint(self.pair, self.members, children, self.creds["ASTER_MASTER"]),
            "before_receipts": {"long": [], "short": []}}
        if transaction:
            record["transaction_id"] = "123"
        if status == "acknowledged":
            record["acknowledged_at"] = self.wall - .5
        self.store.put("pair_margin:gold", {"pending": record, "last_transfer": deepcopy(record)})
        return record

    def test_new_transfer_admission_rejects_whole_80_weight_group_without_requests(self):
        self.budget.reserve(1060)  # 77 ordinary weight remains, less than 80.
        result = self.tick()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["api_notice"]["kind"], "budget")
        self.assertIn("80", result["api_notice"]["text"])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.budget.snapshot()["used"], 1060)
        self.assertEqual(self.transfers, [])
        self.assertNotIn("retry_after", self.state())
        self.assertEqual(result["retry_after"], 60)
        self.advance(5)
        restarted = MarginBalancer(self.engine).tick(self.pair, self.snapshots)
        self.assertEqual(restarted["api_notice"], result["api_notice"])
        self.assertEqual(restarted["retry_after"], 55)
        self.assertEqual(self.requests, [])

    def test_preflight_includes_replacement_snapshots_before_reading_anything(self):
        for current in self.snapshots.values():
            del current.account_read_generation
        self.budget.reserve(914)  # 223 remains; 80+72+72 cannot fit.
        result = self.tick()
        self.assertEqual(result["api_notice"]["kind"], "budget")
        self.assertIn("224", result["api_notice"]["text"])
        self.assertEqual(self.requests, [])
        self.assertEqual(self.refreshed, [])

    def test_preflight_includes_archived_order_checks(self):
        self.store.put("pair_runtime:gold", {"recovery_watch": {"batches": [{"legs": [
            {"key": "long"}, {"key": "short"}]}]}})
        self.budget.reserve(1056)  # 81 remains; two watch queries need 82.
        result = self.tick()
        self.assertEqual(result["api_notice"]["kind"], "budget")
        self.assertIn("82", result["api_notice"]["text"])
        self.assertEqual(self.requests, [])

    def test_archived_order_query_cannot_borrow_recovery_reserve_for_new_transfer(self):
        self.store.put("pair_runtime:gold", {"recovery_watch": {"batches": [{"legs": [
            {"key": "long"}, {"key": "short"}]}]}})
        self.budget.reserve(1055)  # The complete 82-weight preparation fits.

        def guard(engine, pair):
            self.budget.reserve(7)  # Other work fills the limit after preparation.
            with self.brokers["long"].reconciliation_budget():
                self.record("long", "archived-order-query", 1)

        with patch("trading.pair_recovery.require_archived_orders_clear", side_effect=guard):
            result = self.tick()
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["api_notice"]["kind"], "budget")
        self.assertEqual(sum(request[2] for request in self.requests), 75)
        self.assertFalse(any(path == "archived-order-query" for _, path, _, _ in self.requests))
        self.assertEqual(self.budget.snapshot()["used"], 1137)
        self.assertEqual(self.transfers, [])

    def test_new_transfer_uses_ordinary_budget_then_confirmation_uses_reserve(self):
        self.budget.reserve(1057)  # Exactly 80 ordinary weight remains.
        result = self.tick()
        self.assertEqual(result["status"], "acknowledged", result)
        self.assertIsNone(result["pending"])
        self.assertIsNone(result["api_notice"])
        ordinary = [request for request in self.requests if request[1] != "snapshot"]
        recovery = [request for request in self.requests if request[1] == "snapshot"]
        self.assertEqual(sum(request[2] for request in ordinary), 80)
        self.assertEqual(len(recovery), 2)
        self.assertTrue(all(flags[1] and not flags[0] for *_, flags in ordinary))
        self.assertTrue(all(flags[0] and not flags[1] for *_, flags in recovery))
        self.assertEqual(len(self.transfers), 1)
        self.assertEqual(self.budget.snapshot()["used"], 1277)

    def test_income_recovery_uses_reserve_for_both_members(self):
        self.pending()
        self.budget.reserve(1137)
        result = self.tick()
        self.assertEqual(result["status"], "accepted", result)
        self.assertIsNotNone(result["pending"])
        self.assertIsNone(result["api_notice"])
        self.assertEqual([row[2] for row in self.requests], [30, 30])
        self.assertTrue(all(flags[0] for *_, flags in self.requests))
        self.assertEqual(self.budget.snapshot()["used"], 1197)
        self.assertEqual(self.transfers, [])

    def test_income_recovery_group_precheck_does_not_spend_last_50_weight(self):
        pending = self.pending()
        with self.budget.reconciliation():
            self.budget.reserve(1750)
        result = self.tick()
        self.assertEqual(result["api_notice"]["kind"], "budget")
        self.assertEqual(result["pending"]["request_id"], pending["request_id"])
        self.assertEqual(self.requests, [])
        self.advance(10)
        self.tick()
        self.assertEqual(self.requests, [])
        self.assertEqual(self.budget.snapshot()["used"], 1750)
        self.assertEqual(self.transfers, [])

    def test_acknowledged_refresh_group_waits_without_partial_snapshot_reads(self):
        self.pending("acknowledged")
        with self.budget.reconciliation():
            self.budget.reserve(1700)
        result = self.tick()
        self.assertEqual(result["status"], "acknowledged")
        self.assertEqual(result["api_notice"]["kind"], "budget")
        self.assertEqual(self.requests, [])
        self.assertEqual(self.refreshed, [])
        self.assertIsNotNone(self.state()["pending"])
        self.assertEqual(self.transfers, [])
        self.advance(61)
        recovered = self.tick()
        self.assertEqual(recovered["status"], "acknowledged")
        self.assertIsNone(recovered["pending"])
        self.assertIsNone(recovered["api_notice"])
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(all(path == "snapshot" for _, path, _, _ in self.requests))
        self.assertNotIn("api_notice", self.state())
        self.assertEqual(self.transfers, [])

    def test_pending_recovery_honors_exchange_cooldown_even_with_reserve(self):
        self.pending()
        self.budget.block(40, reason="HTTP 429")
        result = self.tick()
        self.assertEqual(result["api_notice"]["kind"], "cooldown")
        self.assertEqual(result["retry_after"], 40)
        self.advance(5)
        self.assertEqual(self.tick()["retry_after"], 35)
        self.assertEqual(self.requests, [])
        self.assertIsNotNone(self.state()["pending"])

    def test_acknowledged_refresh_rechecks_first_generation_before_releasing_pending(self):
        self.pending("acknowledged")
        original = self.brokers["short"].snapshot

        def change_first_account(*args, **kwargs):
            current = original(*args, **kwargs)
            self.brokers["long"]._cycle_account_event("ACCOUNT_UPDATE")
            return current

        with patch.object(self.brokers["short"], "snapshot", side_effect=change_first_account):
            result = self.tick()
        self.assertEqual(result["status"], "acknowledged")
        self.assertIsNotNone(result["pending"])
        self.assertIn("失效", result["reason"])
        self.assertEqual(self.transfers, [])

    def test_post_budget_denial_is_not_sent_and_preserves_deadline_and_notice(self):
        def other_work_used_budget():
            self.budget.reserve(1062)  # Preparation spent 75; fill ordinary 1137.
        self.before_transfer = other_work_used_budget
        result = self.tick()
        self.assertEqual(result["status"], "rejected", result)
        self.assertIsNone(result["pending"])
        self.assertEqual(result["api_notice"]["kind"], "budget")
        self.assertEqual(result["retry_after"], 60)
        self.assertEqual(sum(row[2] for row in self.requests), 75)
        self.assertEqual(self.transfers, [])
        self.assertEqual(self.state()["last_transfer"]["status"], "rejected")
        self.advance(31)  # Ordinary transfer cooldown ends, API wait does not.
        self.assertEqual(self.tick()["retry_after"], 29)
        self.assertEqual(sum(row[2] for row in self.requests), 75)

    def test_unknown_rate_limited_post_stays_pending_and_is_never_repeated(self):
        self.response = AmbiguousOrder("rate limited", retry_after=90, http_status=429)
        result = self.tick()
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["api_notice"]["kind"], "rate_limit")
        self.assertEqual(result["retry_after"], 90)
        requests = list(self.requests)
        self.advance(91)
        result = self.tick()
        self.assertEqual(result["status"], "unknown")
        self.assertIsNotNone(result["pending"])
        self.assertEqual(self.requests, requests)
        self.assertEqual(len(self.transfers), 1)

    def test_paused_disabled_and_waiting_views_preserve_notice_until_successful_check(self):
        notice = {"kind": "budget", "text": "本地执行 API 请求权重预算不足"}
        self.store.put("pair_margin:gold", {"api_notice": notice, "blocked_reason": notice["text"],
                                           "next_check_at": self.wall + 30})
        for enabled, margin_enabled, status in ((False, True, "paused"), (True, False, "disabled")):
            self.pair["enabled"], self.pair["margin"]["enabled"] = enabled, margin_enabled
            view = self.tick()
            self.assertEqual(view["status"], status)
            self.assertEqual(view["api_notice"], notice)
            self.assertEqual(MarginBalancer.status_view(self.pair, self.state())["api_notice"], notice)
        self.pair["enabled"] = self.pair["margin"]["enabled"] = True
        self.advance(31)
        self.snapshots = {side: fixtures.snapshot(2000) for side in self.members}
        for side, current in self.snapshots.items():
            current.account_read_generation = self.brokers[side]._snapshot_generation
        view = self.tick()
        self.assertEqual(view["status"], "waiting")
        self.assertIsNone(view["api_notice"])
        self.assertNotIn("api_notice", self.state())
        self.assertNotIn("blocked_reason", self.state())
        self.assertNotIn("retry_after", view)
        self.assertEqual(self.requests, [])

    def test_nonfinite_retry_cannot_corrupt_durable_deadline(self):
        self.pending()
        with patch.object(self.balancer, "_income", side_effect=BudgetWait("本地 API 请求权重预算不足", retry_after=float("inf"))):
            result = self.tick()
        self.assertEqual(result["retry_after"], 5)
        self.assertEqual(self.state()["next_check_at"], self.wall + 5)
        self.assertNotIn("retry_after", self.state())
