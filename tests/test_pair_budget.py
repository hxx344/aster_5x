"""Real shared budgets and live broker paths backed only by the paper ledger."""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_trading as fixtures
from trading.exchange import BudgetWait, ExchangeError, LiveBroker, RateBudget, RequestNotSent
from trading.execution import Executor
from trading.models import AccountModeError, TradingError, dec, wire
from trading.pair_execution import runtime_default
from trading.paper import PAPER_BRACKETS


SYMBOL = "XAUUSD1"
DUAL = "/fapi/v3/positionSide/dual"
MULTI = "/fapi/v3/multiAssetsMargin"


class OfflineAPI:
    def __init__(self, paper, budget, identity):
        self.paper, self.budget = paper, budget
        self.credentials = {"user": f"0x{identity:040x}", "signer": f"0x{identity + 10:040x}"}
        self.calls = []
        self.hedge = True

    def call(self, method, path, params=None, *, signed=False, weight=1, timeout=None):
        self.budget.reserve(weight)
        self.calls.append((method, path, self.budget._priority_flags()))
        if path == "/fapi/v3/order":
            if method == "POST":
                return self.paper.submit([params])[0]
            return self.paper.query(params["symbol"], params["origClientOrderId"])
        if path == "/fapi/v3/leverage":
            return self.paper.set_leverage(params["symbol"], int(params["leverage"]))
        if path == DUAL:
            return {"dualSidePosition": self.hedge}
        if path == MULTI:
            return {"multiAssetsMargin": False}
        if path == "/fapi/v3/leverageBracket":
            return {"symbol": SYMBOL, "brackets": deepcopy(PAPER_BRACKETS)}
        snapshot = self.paper.snapshot([SYMBOL])
        positions = [{"symbol": p.symbol, "positionSide": p.side,
                      "positionAmt": wire(p.qty if p.side == "LONG" else -p.qty),
                      "entryPrice": wire(p.entry), "markPrice": wire(p.mark),
                      "leverage": str(p.leverage), "isolated": False,
                      "unrealizedProfit": wire(p.unrealized), "maxNotional": "10000000"}
                     for p in snapshot.positions]
        if path == "/fapi/v3/accountWithJoinMargin":
            return {"canTrade": True, "positions": positions,
                    "assets": [{"asset": "USD1", "crossWalletBalance": wire(snapshot.wallet),
                                "crossUnPnl": wire(snapshot.unrealized), "maintMargin": wire(snapshot.maintenance),
                                "availableBalance": wire(snapshot.available)}]}
        if path == "/fapi/v3/positionRisk":
            return [{**row, "marginType": "cross", "unRealizedProfit": row["unrealizedProfit"],
                     "liquidationPrice": "0"} for row in positions]
        raise AssertionError((method, path))


class PairBudgetTests(TestCase):
    setUp = fixtures.PairTradingTests.setUp
    tick = fixtures.PairTradingTests.tick
    expire = fixtures.PairTradingTests.expire

    def live_brokers(self):
        self.budget = RateBudget()
        self.paper = getattr(self, "paper", self.brokers)
        self.brokers = {key: LiveBroker({}, self.market, api=OfflineAPI(broker, self.budget, index))
                        for index, (key, broker) in enumerate(self.paper.items(), 1)}
        self.engine.brokers.update(self.brokers)
        state = self.store.get("pair_runtime:gold")
        if state:
            state.pop("identities", None)
            self.store.put("pair_runtime:gold", state)

    def legs(self, *, reducing=False):
        return [{"key": key, "order": Executor.order(SYMBOL, side,
                    "SELL" if (side == "LONG") == reducing else "BUY", dec("0.001"), "proof_" + key),
                 "receipt": None, "dispatch": "prepared"}
                for key, side in (("long", "LONG"), ("short", "SHORT"))]

    def test_ordinary_polls_reuse_modes_and_events_revoke_cached_authority(self):
        self.live_brokers()
        self.trader._read(self.brokers)
        before = self.budget.snapshot()["used"]
        snapshots, guards = self.trader._read(self.brokers)
        self.assertEqual(self.budget.snapshot()["used"] - before, 20)
        for broker in self.brokers.values():
            self.assertEqual(sum(path == DUAL for _, path, _ in broker.api.calls), 1)
            self.assertEqual(sum(path == MULTI for _, path, _ in broker.api.calls), 1)
        broker = self.brokers["long"]
        broker.api.hedge = False
        broker._cycle_account_event("ACCOUNT_CONFIG_UPDATE")
        with self.assertRaises(TradingError):
            guards["long"]()
        fresh, _ = self.trader._read(self.brokers)
        with self.assertRaises(AccountModeError):
            fresh["long"].require_modes([SYMBOL])
        self.assertEqual(sum(path == DUAL for _, path, _ in broker.api.calls), 2)

    def test_recovery_snapshots_use_reserve_inside_both_workers(self):
        self.live_brokers()
        self.budget.reserve(1500)
        with self.assertRaises(BudgetWait):
            self.trader._read(self.brokers)
        snapshots, guards = self.trader._read(self.brokers, reconciliation=True)
        self.assertEqual(set(snapshots), {"long", "short"})
        self.assertEqual(self.budget.snapshot()["used"], 1642)
        for broker in self.brokers.values():
            self.assertTrue(all(flags[0] for _, _, flags in broker.api.calls))
        for guard in guards.values():
            guard()

    def test_due_close_uses_reserve_without_a_hot_snapshot(self):
        self.assertEqual(self.tick()["phase"], "holding")
        self.expire()
        self.live_brokers()
        self.budget.reserve(1500)
        with patch.object(self.brokers["long"], "cycle_hot_snapshot", side_effect=AssertionError("stale hot data")), \
             patch.object(self.brokers["short"], "cycle_hot_snapshot", side_effect=AssertionError("stale hot data")):
            state = self.tick()
        self.assertIsNone(state["pending"], state)
        self.assertEqual(state["progress"]["completed_cycles"], 1)
        self.assertLessEqual(self.budget.snapshot()["used"], 1800)
        for key, broker in self.brokers.items():
            self.assertTrue(all(flags[0] for _, _, flags in broker.api.calls))
            self.assertEqual(dec(self.paper[key].state["positions"][SYMBOL + ":" + key.upper()]["qty"]), 0)

    def test_opening_cannot_spend_recovery_reserve(self):
        self.live_brokers()
        self.budget.reserve(1500)
        legs = self.legs()
        # Even a caller's ambient priority cannot leak to newly spawned workers.
        with self.budget.reconciliation():
            self.trader._dispatch(self.pair, runtime_default(), self.brokers, legs)
        self.assertTrue(all(leg["receipt"]["local_not_sent"] for leg in legs))
        self.assertEqual(self.budget.snapshot()["used"], 1500)
        self.assertFalse(any(broker.api.calls for broker in self.brokers.values()))

    def test_partial_open_is_reduced_from_reserved_budget(self):
        original = self.brokers["long"].submit

        def no_repair_yet(orders):
            if orders[0]["side"] == "SELL":
                raise RequestNotSent("ordinary quota unavailable")
            return original(orders)

        with patch.object(self.brokers["long"], "submit", side_effect=no_repair_yet), \
             patch.object(self.brokers["short"], "submit", side_effect=ExchangeError("rejected", code=-2019)):
            self.assertIsNotNone(self.tick()["pending"])
        self.live_brokers()
        state = self.store.get("pair_runtime:gold")
        state["pending"]["identities"] = self.trader._members(self.pair)[2]
        state["pending"]["repair_retry_at"] = 0
        self.store.put("pair_runtime:gold", state)
        self.budget.reserve(1500)
        self.assertEqual(self.tick()["phase"], "repairing")
        state = self.tick()
        self.assertIsNone(state["pending"], state)
        self.assertFalse(state["last_batch"]["completed"])
        self.assertEqual(dec(self.paper["long"].state["positions"][SYMBOL + ":LONG"]["qty"]), 0)
        self.assertLessEqual(self.budget.snapshot()["used"], 1800)
        self.assertTrue(all(flags[0] for broker in self.brokers.values() for _, _, flags in broker.api.calls))

    def test_leverage_confirmation_uses_reserve_without_resending(self):
        self.live_brokers()
        state = runtime_default()
        state["pending"] = {"kind": "leverage", "identities": self.trader._members(self.pair)[2],
                            "before": {"LONG": "0", "SHORT": "0"}, "target_leverage": 5,
                            "previous_leverage": 2, "results": {}}
        self.store.put("pair_runtime:gold", state)
        self.budget.reserve(1500)
        state = self.tick()
        self.assertIsNone(state["pending"], state)
        self.assertTrue(all(method == "GET" and flags[0]
                            for broker in self.brokers.values() for method, _, flags in broker.api.calls))

    def test_repair_cannot_bypass_total_limit_or_exchange_cooldown(self):
        for blocked in ("total", "cooldown"):
            with self.subTest(blocked=blocked):
                self.live_brokers()
                if blocked == "total":
                    with self.budget.reconciliation():
                        self.budget.reserve(1800)
                else:
                    self.budget.block(30, reason="HTTP 429")
                with self.assertRaises(RequestNotSent):
                    self.trader._read(self.brokers, reconciliation=True)
                legs = self.legs(reducing=True)
                self.trader._dispatch(self.pair, runtime_default(), self.brokers, legs, reconciliation=True)
                self.assertTrue(all(leg["receipt"]["local_not_sent"] for leg in legs))
                self.assertFalse(any(broker.api.calls for broker in self.brokers.values()))

    def test_current_retry_delay_is_returned_but_not_persisted_or_reused(self):
        with patch.object(self.trader, "_read", side_effect=RequestNotSent("cooldown", retry_after=17)):
            state = self.tick()
        self.assertEqual(state["retry_after"], 17)
        self.assertNotIn("retry_after", self.store.get("pair_runtime:gold"))
        self.assertNotIn("retry_after", self.tick())
