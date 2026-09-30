"""Exercise the collateral lifeline through live brokers without real networks."""
from contextlib import ExitStack
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch
from urllib.parse import parse_qsl

from eth_account import Account
import httpx

from tests import test_pair_trading as fixtures
from tests.test_pair_budget import OfflineAPI
from trading.account_cache import HotAccountUnavailable
from trading.exchange import ExchangeError, LiveBroker, RateBudget
from trading.margin_balance import TransferAPI
from trading.models import dec, wire


SYMBOL = "XAUUSD1"
ACCOUNT = "/fapi/v3/accountWithJoinMargin"
BRACKET = "/fapi/v3/leverageBracket"
INCOME = "/fapi/v3/income"
TRANSFER = "/fapi/v3/subAccountTransfer"


class _CollateralAPI(OfflineAPI):
    """Exchange positions and balances backed by an isolated paper ledger."""

    def __init__(self, paper, budget, identity, credentials):
        super().__init__(paper, budget, identity)
        self.identity = identity
        self.credentials = credentials
        self.fail_brackets = False

    def call(self, method, path, params=None, *, signed=False, weight=1, timeout=None):
        if path == INCOME or path == BRACKET and self.fail_brackets:
            self.budget.reserve(weight)
            self.calls.append((method, path, self.budget._priority_flags()))
            if path == BRACKET:
                raise ExchangeError("opening tiers unavailable")
            return []
        value = super().call(method, path, params, signed=signed, weight=weight, timeout=timeout)
        if path == ACCOUNT:
            snapshot = self.paper.snapshot([SYMBOL])
            value["accountId"] = self.identity
            value["assets"][0].update(marginBalance=wire(snapshot.equity),
                walletBalance=wire(snapshot.wallet), maxWithdrawAmount=wire(max(dec(0), snapshot.available)))
        return value


class PairMarginLifelineLiveTests(TestCase):
    def setUp(self):
        fixtures.PairTradingTests.setUp(self)
        self.margin.stop()  # The actual funds state machine is under test.
        self.paper = self.brokers
        self.budget = RateBudget()
        self.credentials = {}
        for index, side in enumerate(("long", "short", "master"), 1):
            key = bytes([index]) * 32  # Public deterministic test keys only.
            self.credentials["ASTER_" + side.upper()] = {
                "user": "0x" + str(index) * 40,
                "signer": Account.from_key(key).address,
                "private_key": key,
            }
        self.brokers = {}
        for index, (side, paper) in enumerate(self.paper.items(), 10):
            paper.state["wallet"] = "3000" if side == "long" else "1000"
            paper.save()
            member = self.store.account(side)
            self.store.save_account({**member, "mode": "live"})
            api = _CollateralAPI(paper, self.budget, index, self.credentials[member["env_prefix"]])
            self.brokers[side] = LiveBroker({}, self.market, api=api)
        self.engine.brokers.update(self.brokers)
        self.engine.pairs.trader = self.trader
        self.pair["ordinary"]["enabled"] = True
        self.pair["cycle"]["enabled"] = False
        self.pair["margin"].update(enabled=True, master_env_prefix="ASTER_MASTER")
        self.pair = self.store.save_pair(self.pair)
        self.sent = []
        self.signed = []
        self.on_signed = lambda: None
        self.master_calls = []
        owner = self

        class Master:
            def __init__(self, credentials, *, budget):
                self.budget = budget

            def call(self, method, path, *, signed=False, weight=1):
                owner.assertEqual((method, path), ("GET", "/fapi/v3/getSubAccountList"))
                self.budget.reserve(weight)
                owner.master_calls.append((method, path))
                return [{"accountId": 1, "parentAccount": True}] + [
                    {"accountId": broker.api.identity, "parentAccount": False,
                     "sourceAddr": broker.api.credentials["user"]}
                    for broker in owner.brokers.values()
                ]

            def close(self):
                pass

        def transfer_api(credentials, *, budget, before_submit):
            api = TransferAPI(credentials, budget=budget, before_submit=before_submit,
                              transport=httpx.MockTransport(self.transport))
            sign = api.signed_parameters

            def signed_parameters(params):
                result = sign(params)
                self.signed.append(dict(params))
                self.on_signed()
                return result

            api.signed_parameters = signed_parameters
            return api

        for target, replacement in (
                ("trading.margin_balance.credentials_for", lambda prefix: deepcopy(self.credentials[prefix])),
                ("trading.margin_balance.API", Master),
                ("trading.margin_balance.TransferAPI", transfer_api),
                ("trading.engine.Engine.live_allowed", lambda engine, member: True)):
            mocked = patch(target, replacement)
            mocked.start()
            self.addCleanup(mocked.stop)

    def transport(self, request):
        self.assertEqual((request.method, request.url.path), ("POST", TRANSFER))
        pending = self.store.get("pair_margin:gold")["pending"]
        self.assertEqual(pending["status"], "submitting")
        params = dict(parse_qsl(request.content.decode()))
        self.assertEqual(params["amount"], pending["amount"])
        self.assertEqual(params["asset"], "USD1")
        self.assertTrue(params["signature"])
        self.assertEqual(params["fromAccountAddress"], self.brokers[pending["source"]].api.credentials["user"])
        self.assertEqual(params["toAccountAddress"], self.brokers[pending["destination"]].api.credentials["user"])
        self.sent.append(params)
        # Simulate exchange acceptance by changing the actual ledger queried
        # by the following live-broker confirmation reads.
        for side, sign in ((pending["source"], -1), (pending["destination"], 1)):
            paper = self.paper[side]
            paper.state["wallet"] = wire(dec(paper.state["wallet"]) + sign * dec(params["amount"]))
            paper.save()
        return httpx.Response(200, json={"code": 200, "msg": "success"})

    def tick(self):
        self.engine.pairs.tick(self.pair["id"])
        return self.store.get("pair_runtime:gold")

    def assert_no_trades(self):
        self.assertTrue(all(method == "GET" for broker in self.brokers.values()
                            for method, _, _ in broker.api.calls))
        self.assertTrue(all(not paper.state["orders"] for paper in self.paper.values()))

    def assert_transferred(self, state):
        self.assertEqual(len(self.sent), 1, state)
        amount = dec(self.sent[0]["amount"])
        self.assertGreater(amount, 0)
        self.assertEqual(dec(self.paper["long"].state["wallet"]), dec(3000) - amount)
        self.assertEqual(dec(self.paper["short"].state["wallet"]), dec(1000) + amount)
        journal = self.store.get("pair_margin:gold")
        self.assertIsNone(journal["pending"], journal)
        self.assertEqual(journal["last_transfer"]["status"], "acknowledged")
        self.assertEqual(journal["last_transfer"]["refresh_source"], "rest")
        self.assertTrue(state["margin"]["blocks_trading"])
        self.assertEqual(state["owned"], {"LONG": "0", "SHORT": "0"})
        self.assert_no_trades()

    def test_static_leverage_mismatch_still_transfers_without_trading(self):
        self.paper["short"].state["leverages"][SYMBOL] = 10
        self.paper["short"].save()
        self.assert_transferred(self.tick())
        state = self.tick()
        self.assertEqual(state["phase"], "attention", state)
        self.assertIn("杠杆不一致", state["reason"])
        self.assertEqual(len(self.sent), 1)
        self.assert_no_trades()

    def test_untracked_quantity_still_transfers_without_adopting_or_trading(self):
        position = self.paper["long"].state["positions"][SYMBOL + ":LONG"]
        position.update(qty="0.001", entry=wire(self.market.book(SYMBOL).mark))
        self.paper["long"].save()
        self.assert_transferred(self.tick())
        state = self.tick()
        self.assertEqual(state["phase"], "attention", state)
        self.assertIn("账本不一致", state["reason"])
        self.assertEqual(state["owned"], {"LONG": "0", "SHORT": "0"})
        self.assertEqual(self.paper["long"].state["positions"][SYMBOL + ":LONG"]["qty"], "0.001")
        self.assertEqual(len(self.sent), 1)
        self.assert_no_trades()

    def test_missing_hot_snapshot_uses_fresh_collateral_reads_and_confirms_transfer(self):
        self.pair["ordinary"]["enabled"] = False
        self.pair["cycle"]["enabled"] = True
        self.pair = self.store.save_pair(self.pair)
        with ExitStack() as stack:
            reads = []
            for broker in self.brokers.values():
                with self.assertRaises(HotAccountUnavailable):
                    broker.cycle_hot_snapshot([SYMBOL])
                reads.append(stack.enter_context(patch.object(broker, "margin_snapshot", wraps=broker.margin_snapshot)))
            self.assert_transferred(self.tick())
            self.assertTrue(all(read.call_count >= 2 for read in reads))
        self.assertFalse(any(path == BRACKET for broker in self.brokers.values() for _, path, _ in broker.api.calls))

    def test_opening_tier_failure_does_not_block_fresh_collateral_or_confirmation(self):
        with ExitStack() as stack:
            reads = []
            for broker in self.brokers.values():
                broker.api.fail_brackets = True
                reads.append(stack.enter_context(patch.object(broker, "margin_snapshot", wraps=broker.margin_snapshot)))
            self.assert_transferred(self.tick())
            self.assertTrue(all(read.call_count >= 2 for read in reads))
        self.assertTrue(any(path == BRACKET for broker in self.brokers.values() for _, path, _ in broker.api.calls))

    def test_event_after_fallback_read_and_real_signing_prevents_transfer_transport(self):
        for broker in self.brokers.values():
            broker.api.fail_brackets = True
        self.on_signed = lambda: self.brokers["short"]._cycle_account_event("ACCOUNT_UPDATE")
        state = self.tick()
        self.assertEqual(len(self.signed), 1, state)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.paper["long"].state["wallet"], "3000")
        self.assertEqual(self.paper["short"].state["wallet"], "1000")
        journal = self.store.get("pair_margin:gold")
        self.assertIsNone(journal["pending"])
        self.assertEqual(journal["last_transfer"]["status"], "rejected")
        self.assertTrue(state["margin"]["blocks_trading"])
        self.assert_no_trades()

    def assert_local_transfer_rejection(self, state):
        self.assertEqual(len(self.signed), 1, state)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.paper["long"].state["wallet"], "3000")
        self.assertEqual(self.paper["short"].state["wallet"], "1000")
        journal = self.store.get("pair_margin:gold")
        self.assertIsNone(journal["pending"], journal)
        self.assertEqual(journal["last_transfer"]["status"], "rejected")
        self.assertEqual(state["margin"]["status"], "rejected")
        self.assertTrue(state["margin"]["blocks_trading"])
        self.assert_no_trades()

    def test_pair_pause_and_revision_after_real_signing_prevent_transfer_transport(self):
        revision = self.pair["revision"]

        def pause():
            current = self.store.pair("gold")
            self.store.save_pair({**current, "enabled": False, "pause_reason": "explicit offline pause"})

        self.on_signed = pause
        self.assert_local_transfer_rejection(self.tick())
        current = self.store.pair("gold")
        self.assertFalse(current["enabled"])
        self.assertGreater(current["revision"], revision)
        self.assertEqual(self.tick()["phase"], "paused")
        self.assertEqual(len(self.signed), 1)
        self.assertEqual(self.sent, [])
        self.assert_no_trades()

    def test_member_identity_after_real_signing_prevents_transfer_transport(self):
        def change_identity():
            broker = self.brokers["short"]
            broker.api.credentials = {**broker.api.credentials, "user": "0x" + "e" * 40}

        self.on_signed = change_identity
        self.assert_local_transfer_rejection(self.tick())
        state = self.tick()
        self.assertEqual(state["phase"], "attention", state)
        self.assertEqual(len(self.signed), 1)
        self.assertEqual(self.sent, [])
        self.assert_no_trades()
