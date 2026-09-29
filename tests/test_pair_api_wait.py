"""Quota preparation and visible wait state, using offline brokers only."""
import time
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_budget as fixtures
from trading.exchange import BudgetWait, ExchangeError, RequestNotSent, api_wait_notice
from trading.models import dec


class PairAPIWaitTests(TestCase):
    setUp = fixtures.PairBudgetTests.setUp
    live_brokers = fixtures.PairBudgetTests.live_brokers
    tick = fixtures.PairBudgetTests.tick
    expire = fixtures.PairBudgetTests.expire

    def ordinary(self):
        self.live_brokers()
        self.pair["cycle"]["enabled"] = False
        self.pair["ordinary"]["enabled"] = True
        self.pair = self.store.save_pair(self.pair)
        self.engine.capacities = lambda symbol: {5: dec(0), 10: dec(0), 20: dec(0)}

    def read_count(self):
        return sum(path == "/fapi/v3/accountWithJoinMargin"
                   for broker in self.brokers.values() for _, path, _ in broker.api.calls)

    def test_shared_read_admission_rejects_before_either_account_spends(self):
        self.ordinary()
        self.budget.reserve(1400)  # One cold read fits, two do not.
        before = self.budget.local_weight
        with self.assertRaises(BudgetWait):
            self.trader._read(self.brokers)
        self.assertEqual(self.budget.local_weight, before)
        self.assertFalse(any(b.api.calls for b in self.brokers.values()))

    def test_paired_mode_evidence_does_not_reserve_an_unneeded_dual_query(self):
        self.ordinary()
        self.trader._read(self.brokers)
        broker = self.brokers["long"]
        broker.cached_at["dual"] = time.monotonic() - 20
        self.assertEqual(broker.snapshot_weight(["XAUUSD1"]) -
                         broker.snapshot_weight(["XAUUSD1"], reuse_account_mode=True), 30)
        broker._cycle_account_event("ACCOUNT_CONFIG_UPDATE")
        self.assertEqual(broker.snapshot_weight(["XAUUSD1"]),
                         broker.snapshot_weight(["XAUUSD1"], reuse_account_mode=True))

    def test_ordinary_wait_preserves_budget_text_without_pending_and_clears_on_success(self):
        self.ordinary()
        message = "本地执行 API 请求权重预算不足（估算已用1393/1137，本次需5）"
        with patch.object(self.trader, "_read", side_effect=BudgetWait(message, retry_after=21)):
            state = self.tick()
        self.assertIsNone(state["pending"])
        self.assertEqual(state["phase"], "waiting")
        self.assertEqual(state["api_notice"], {"kind": "budget", "text": message})
        self.assertEqual(state["retry_after"], 21)
        self.assertEqual(self.store.get("pair_runtime:gold")["api_notice"], state["api_notice"])
        state = self.tick()
        self.assertIsNone(state["api_notice"])
        self.assertNotIn("retry_after", state)

    def test_slow_read_cannot_restart_five_second_observation_at_completion(self):
        self.ordinary()
        self.tick()
        self.assertEqual(self.read_count(), 2)
        observed = self.trader._ordinary_observations["gold"]
        for snapshot in observed[1].values():
            snapshot.timestamp = time.time() - 6
        # Completion monotonic time is still recent. Source age is already six.
        self.tick()
        self.assertEqual(self.read_count(), 4)

    def test_balance_budget_wait_does_not_repeat_private_reads(self):
        self.ordinary()
        self.pair["margin"]["enabled"] = True
        self.pair = self.store.save_pair(self.pair)
        deadline = time.time() + 30
        notice = {"kind": "budget", "text": "本地执行 API 请求权重预算不足"}
        self.store.put("pair_margin:gold", {"api_notice": notice, "blocked_reason": notice["text"],
                                           "next_check_at": deadline})
        with patch.object(self.trader, "_read", side_effect=AssertionError("must not read during backoff")):
            for _ in range(3):
                state = self.tick()
                self.assertEqual(state["phase"], "margin_wait")
                self.assertGreater(state["retry_after"], 20)
        self.assertEqual(self.read_count(), 0)

    def test_transfer_retry_delay_never_delays_due_cycle_reduction(self):
        self.assertEqual(self.tick()["phase"], "holding")
        self.expire()
        notice = {"kind": "budget", "text": "本地执行 API 请求权重预算不足"}
        self.store.put("pair_margin:gold", {"api_notice": notice, "blocked_reason": notice["text"],
                                           "next_check_at": time.time() + 50})
        with patch("trading.margin_balance.MarginBalancer.tick", return_value={
                "blocks_trading": True, "api_notice": notice, "retry_after": 50, "reason": notice["text"]}):
            state = self.tick()
        self.assertEqual(state["progress"]["completed_cycles"], 1)
        self.assertIsNone(state["pending"])
        self.assertNotIn("retry_after", state)

    def test_new_snapshots_are_published_before_slow_balance_preparation(self):
        self.ordinary()
        self.pair["margin"]["enabled"] = True
        self.pair = self.store.save_pair(self.pair)

        def balance(pair, snapshots, **kwargs):
            saved = self.store.get("pair_runtime:gold")
            for side in ("long", "short"):
                self.assertEqual(saved["snapshots"][side]["timestamp"], snapshots[side].timestamp)
            return {"blocks_trading": False}

        with patch("trading.margin_balance.MarginBalancer.tick", side_effect=balance):
            state = self.tick()
        self.assertIsNone(state["pending"])

    def test_safe_wait_classification_does_not_copy_unrelated_remote_error(self):
        self.assertIsNone(api_wait_notice(ExchangeError("untrusted secret=plaintext", code=-2019)))
        self.assertIsNone(api_wait_notice(RequestNotSent("账户状态变化")))
        self.assertEqual(api_wait_notice(RequestNotSent("接口冷却中：HTTP 429", retry_after=5))["kind"], "cooldown")
        result = api_wait_notice(ExchangeError("HTTP 429 secret=plaintext", http_status=429))
        self.assertEqual(result["kind"], "rate_limit")
        self.assertNotIn("plaintext", result["text"])
