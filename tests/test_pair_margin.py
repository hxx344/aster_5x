import copy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qsl, urlencode

from eth_account import Account
from eth_account.messages import encode_typed_data
import httpx

from trading.exchange import AmbiguousOrder, ExchangeError, LiveBroker, RateBudget, RequestNotSent
from trading.margin_balance import DEFAULT_MARGIN, MarginBalancer, TransferAPI, validate_margin
from trading.models import AccountSnapshot, Position, TradingError, dec
from trading.paper import DemoMarket, PaperBroker
from trading.store import Store


class FakeAccountAPI:
    def __init__(self, wallet, account_id):
        self.budget = RateBudget()
        self.calls = []
        self.rows = []
        self.account = {"accountId": account_id, "canTrade": True, "assets": [{"asset": "USD1",
            "availableBalance": str(wallet), "maxWithdrawAmount": str(wallet), "marginBalance": str(wallet),
            "maintMargin": "0"}], "positions": []}

    def call(self, method, path, params=None, **kwargs):
        self.calls.append((method, path, params))
        if path.endswith("/income"):
            return copy.deepcopy(self.rows)
        if path.endswith("/accountWithJoinMargin"):
            return copy.deepcopy(self.account)
        if path.endswith("/openOrders"):
            return []
        raise AssertionError(path)


def snapshot(wallet):
    return AccountSnapshot(dec(wallet), dec(0), dec(wallet), dec(wallet), dec(0), [], [], True, False, True, time.time())


class PairMarginTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state.db")
        self.market = DemoMarket()
        self.members = {side: {"id": side, "name": side, "mode": "paper", "env_prefix": "ASTER_" + side.upper(),
            "enabled": True, "policy": {"symbols": ["XAUUSD1"], "margin_limit": "0.5"}} for side in ("long", "short")}
        for member in self.members.values():
            self.store.save_account(member)
        self.brokers = {side: PaperBroker(side, self.market, self.store) for side in self.members}
        for side, wallet in (("long", "3000"), ("short", "1000")):
            self.brokers[side].state["wallet"] = wallet
            self.brokers[side].save()
        self.engine = SimpleNamespace(store=self.store, broker=lambda member: self.brokers[member["id"]],
            live_allowed=lambda member: True)
        self.balancer = MarginBalancer(self.engine)
        self.pair = {"id": "gold", "symbol": "XAUUSD1", "long_account_id": "long", "short_account_id": "short",
            "enabled": True, "margin": {**DEFAULT_MARGIN, "enabled": True, "master_env_prefix": "ASTER_MASTER"}}

    def snapshots(self):
        return {side: broker.snapshot(["XAUUSD1"]) for side, broker in self.brokers.items()}

    def state(self):
        return self.store.get("pair_margin:gold", {})

    def ready(self):
        state = self.state()
        state["next_check_at"] = 0
        self.store.put("pair_margin:gold", state)

    def live(self):
        self.creds = {}
        self.refresh_error, self.refreshed = None, []
        for index, name in enumerate(("long", "short", "master"), 1):
            key = bytes([index]) * 32  # Deterministic public test keys only.
            self.creds["ASTER_" + name.upper()] = {"user": "0x" + str(index) * 40,
                "signer": Account.from_key(key).address, "private_key": key}
        for side in self.members:
            self.members[side]["mode"] = "live"
            self.store.save_account(self.members[side])
            self.brokers[side] = LiveBroker({}, self.market, api=FakeAccountAPI(3000 if side == "long" else 1000,
                10 if side == "long" else 20))
            def fresh(symbols, fresh_modes=False, side=side):
                self.refreshed.append(side)
                if self.refresh_error:
                    raise self.refresh_error
                current = snapshot(self.brokers[side].api.account["assets"][0]["marginBalance"])
                current.account_read_generation = self.brokers[side]._snapshot_generation
                return current
            self.brokers[side].snapshot = fresh
        self.listing = [{"accountId": 1, "parentAccount": True},
            {"accountId": 10, "parentAccount": False, "sourceAddr": self.creds["ASTER_LONG"]["user"]},
            {"accountId": 20, "parentAccount": False, "sourceAddr": self.creds["ASTER_SHORT"]["user"]}]
        self.master_calls, self.transfers = [], []
        self.response = {"code": 200, "msg": "success"}
        owner = self

        class Master:
            def __init__(self, *args, **kwargs):
                self.budget = RateBudget()
                self.before_submit = kwargs.get("before_submit")

            def call(self, method, path, *args, **kwargs):
                owner.master_calls.append((method, path))
                return copy.deepcopy(owner.listing)

            def close(self):
                pass

        class Transfer(Master):
            def call(self, method, path, params, **kwargs):
                persisted = owner.state()["pending"]
                owner.assertEqual(persisted["status"], "submitting")
                TransferAPI._before_request(self, SimpleNamespace(method=method))
                owner.transfers.append((method, path, copy.deepcopy(params)))
                if isinstance(owner.response, Exception):
                    raise owner.response
                if owner.response.get("code") == 200 and owner.response.get("msg") == "success":
                    for side, sign in ((persisted["source"], -1), (persisted["destination"], 1)):
                        asset = owner.brokers[side].api.account["assets"][0]
                        for field in ("marginBalance", "availableBalance"):
                            asset[field] = str(dec(asset[field]) + dec(persisted["amount"]) * sign)
                return copy.deepcopy(owner.response)

        for target, value in (("credentials_for", lambda prefix: copy.deepcopy(self.creds[prefix])),
                              ("API", Master), ("TransferAPI", Transfer)):
            patcher = patch("trading.margin_balance." + target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        snapshots = {"long": snapshot(3000), "short": snapshot(1000)}
        for current in snapshots.values():
            current.account_read_generation = 0
        return snapshots

    def receipts(self, txn="123", kind="SUBUSER_ASSET_TRANSFER", amount=None):
        record = self.state()["pending"]
        amount = dec(amount or record["amount"])
        for side, sign in ((record["source"], -1), (record["destination"], 1)):
            self.brokers[side].api.rows = [{"incomeType": kind, "asset": "USD1", "income": str(amount * sign),
                "tranId": txn, "time": int(record["created_at"]) * 1000}]

    def legacy_pending(self, snapshots, *, status="accepted"):
        self.refresh_error = ExchangeError("test balance refresh failure")
        self.balancer.tick(self.pair, snapshots)
        self.refresh_error = None
        state = self.state()
        state["pending"]["status"] = status
        state["last_transfer"] = copy.deepcopy(state["pending"])
        self.store.put("pair_margin:gold", state)

    def test_config_defaults_and_invalid_values(self):
        self.assertEqual(validate_margin({}), DEFAULT_MARGIN)
        for change in ({"enabled": "true"}, {"buffer_ratio": "NaN"}, {"buffer_ratio": "1"},
                       {"master_env_prefix": "PRIVATE-KEY"}, {"min_transfer": "2", "max_transfer": "1"},
                       {"check_interval_seconds": True}, {"cooldown_seconds": float("inf")}, {"private_key": "bad"}):
            with self.subTest(change=change), self.assertRaises(TradingError):
                validate_margin(change)

    def test_disabled_and_paused_never_change_wallets(self):
        self.pair["margin"]["enabled"] = False
        self.assertEqual(self.balancer.tick(self.pair, {})["status"], "disabled")
        self.pair["margin"]["enabled"] = True
        self.pair["enabled"] = False
        self.assertEqual(self.balancer.tick(self.pair, {})["status"], "paused")
        self.assertEqual(self.brokers["long"].state["wallet"], "3000")

    def test_paper_transfer_is_atomic_and_persists_after_restart(self):
        result = self.balancer.tick(self.pair, self.snapshots())
        self.assertEqual(result["status"], "paper_confirmed")
        self.assertTrue(result["blocks_trading"])
        self.assertEqual(result["last_transfer"]["amount"], "1000.00000000")
        self.assertEqual(self.brokers["long"].state["wallet"], "2000.00000000")
        self.assertEqual(self.brokers["short"].state["wallet"], "2000.00000000")
        restarted = MarginBalancer(self.engine).tick(self.pair, self.snapshots())
        self.assertEqual(restarted["status"], "cooldown")
        self.assertFalse(restarted["blocks_trading"])
        self.assertEqual(PaperBroker("long", self.market, self.store).state["wallet"], "2000.00000000")

    def test_paper_second_wallet_failure_rolls_back_both_sides_and_receipt(self):
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_short BEFORE UPDATE ON kv WHEN NEW.key='paper:short' "
                       "BEGIN SELECT RAISE(ABORT, 'test disk failure'); END")
        result = self.balancer.tick(self.pair, self.snapshots())
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.store.get("paper:long")["wallet"], "3000")
        self.assertEqual(self.store.get("paper:short")["wallet"], "1000")
        self.assertFalse(self.state().get("last_transfer"))

    def test_paper_stale_wallet_is_not_debited(self):
        snapshots = self.snapshots()
        self.brokers["long"].state["wallet"] = "500"
        self.brokers["long"].save()
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.store.get("paper:long")["wallet"], "500")
        self.assertEqual(self.store.get("paper:short")["wallet"], "1000")

    def test_threshold_units_minimum_round_down_and_buffer(self):
        config = validate_margin({"threshold": "10", "min_transfer": "1", "max_transfer": "1000"})
        snapshots = {"long": snapshot(100), "short": snapshot(90)}
        self.assertIsNone(self.balancer._plan(self.pair, self.members, snapshots, config, {"long": 100, "short": 90}))
        snapshots["short"].available = dec("80")
        plan = self.balancer._plan(self.pair, self.members, snapshots, config, {"long": "3.000000019", "short": 80})
        self.assertEqual(plan["amount"], "3.00000001")
        snapshots["long"].positions = [Position("XAUUSD1", "LONG", dec(1), dec(100), dec(100), 3)]
        snapshots["short"].available = dec(0)
        plan = self.balancer._plan(self.pair, self.members, snapshots, config, {"long": 100, "short": 0})
        self.assertLessEqual(dec(plan["amount"]), dec("25.92592593"))
        self.assertLessEqual(snapshots["long"].occupied_margin / (dec(100) - dec(plan["amount"])), dec("0.45"))

    def test_transfer_respects_stricter_pair_and_member_margin_limits(self):
        self.pair["ordinary"] = {"margin_limit": "0.3"}
        snapshots = {"long": snapshot(100), "short": snapshot(0)}
        snapshots["long"].positions = [Position("XAUUSD1", "LONG", dec(1), dec(100), dec(100), 5)]
        config = validate_margin(self.pair["margin"])
        plan = self.balancer._plan(self.pair, self.members, snapshots, config, {"long": 100, "short": 0})
        self.assertEqual(plan["amount"], "20.00000000")
        self.pair["ordinary"]["margin_limit"] = "0.8"
        self.members["long"]["policy"]["margin_limit"] = "0.3"
        same = self.balancer._plan(self.pair, self.members, snapshots, config, {"long": 100, "short": 0})
        self.assertEqual(same["amount"], "20.00000000")

    def test_wrong_direction_isolated_modes_pending_orders_and_staleness_block(self):
        valid = self.snapshots()
        changes = [replace(valid["long"], hedge_mode=False), replace(valid["long"], multi_assets=True),
            replace(valid["long"], timestamp=time.time() - 30), replace(valid["long"], open_orders=[{"orderId": "external"}]),
            replace(valid["long"], positions=[Position("XAUUSD1", "SHORT", dec(1), dec(10), dec(10), 5)]),
            replace(valid["long"], positions=[Position("XAUUSD1", "LONG", dec(1), dec(10), dec(10), 5, isolated=True)])]
        for changed in changes:
            self.ready()
            result = self.balancer.tick(self.pair, {**valid, "long": changed})
            self.assertTrue(result["blocks_trading"])
        self.assertEqual(self.balancer.tick(self.pair, valid, pending_orders=True)["status"], "waiting")
        self.assertFalse(self.state().get("last_transfer"))

    def test_ownership_is_verified_by_address_or_authenticated_account_id(self):
        self.live()
        self.assertEqual(self.balancer.verify_members(self.pair), {"verified": True, "mode": "live"})
        for row in self.listing:
            row.pop("sourceAddr", None)
        self.assertTrue(self.balancer.verify_members(self.pair)["verified"])
        self.brokers["short"].api.account.pop("accountId")
        with self.assertRaisesRegex(TradingError, "无法核实"):
            self.balancer.verify_members(self.pair)
        self.assertEqual(self.transfers, [])

    def test_missing_membership_and_wallet_conflicts_never_submit(self):
        snapshots = self.live()
        self.listing[2]["sourceAddr"] = "0x" + "9" * 40
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.transfers, [])

    def test_frozen_child_or_master_used_as_child_is_rejected(self):
        self.live()
        self.listing[2]["subAccountStatus"] = "FREEZE"
        with self.assertRaisesRegex(TradingError, "冻结"):
            self.balancer.verify_members(self.pair)
        self.listing[2].pop("subAccountStatus")
        self.creds["ASTER_SHORT"]["user"] = self.creds["ASTER_MASTER"]["user"]
        with self.assertRaisesRegex(TradingError, "两个不同"):
            self.balancer.verify_members(self.pair)

    def test_live_intent_precedes_submit_and_no_credentials_are_persisted(self):
        snapshots = self.live()
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "acknowledged")
        self.assertTrue(result["blocks_trading"])
        method, path, params = self.transfers[0]
        self.assertEqual(params["fromAccountAddress"], self.creds["ASTER_LONG"]["user"])
        self.assertEqual(params["toAccountAddress"], self.creds["ASTER_SHORT"]["user"])
        self.assertNotIn("user", params)
        self.assertEqual(params["asset"], "USD1")
        persisted = str(self.state()) + str(result)
        for values in self.creds.values():
            for name in ("user", "signer"):
                self.assertNotIn(values[name], persisted)
        self.assertNotIn("signature", persisted)

    def test_official_success_without_transaction_id_refreshes_then_releases_pending(self):
        snapshots = self.live()
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "acknowledged")
        self.assertIsNone(result["pending"])
        self.assertTrue(result["blocks_trading"])
        self.assertEqual(self.refreshed, ["long", "short"])
        self.assertIn("余额已刷新", result["reason"])
        self.assertNotIn("transaction_id", result["last_transfer"])
        self.assertGreaterEqual(result["last_transfer"]["refreshed_at"], result["last_transfer"]["acknowledged_at"])
        next_tick = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(next_tick["status"], "cooldown")
        self.assertFalse(next_tick["blocks_trading"])
        self.assertEqual(len(self.transfers), 1)

    def test_acknowledged_refresh_failure_stays_pending_and_restarts_read_only(self):
        snapshots = self.live()
        self.refresh_error = ExchangeError("test refresh unavailable")
        first = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(first["status"], "acknowledged")
        self.assertEqual(self.state()["pending"]["status"], "acknowledged")
        self.assertTrue(first["blocks_trading"])
        self.ready()
        self.pair["enabled"] = False
        self.pair["margin"]["enabled"] = False
        restarted = MarginBalancer(self.engine)
        second = restarted.tick(self.pair, snapshots)
        self.assertEqual(second["status"], "acknowledged")
        self.assertIsNotNone(second["pending"])
        self.assertEqual(len(self.transfers), 1)
        self.refresh_error = None
        self.ready()
        final = restarted.tick(self.pair, snapshots)
        self.assertEqual(final["status"], "acknowledged")
        self.assertIsNone(final["pending"])
        self.assertEqual(len(self.transfers), 1)

    def test_acknowledged_requires_both_post_response_snapshots(self):
        snapshots = self.live()
        original = self.brokers["short"].snapshot
        self.brokers["short"].snapshot = lambda *args, **kwargs: snapshots["short"]
        first = self.balancer.tick(self.pair, snapshots)
        self.assertIsNotNone(first["pending"])
        self.assertEqual(first["status"], "acknowledged")
        self.assertIn("旧快照", first["reason"])
        self.brokers["short"].snapshot = original
        self.ready()
        self.assertIsNone(MarginBalancer(self.engine).tick(self.pair, snapshots)["pending"])
        self.assertEqual(len(self.transfers), 1)

    def test_success_code_without_success_message_stays_unknown(self):
        snapshots = self.live()
        self.response = {"code": 200, "msg": "processing"}
        self.assertEqual(self.balancer.tick(self.pair, snapshots)["status"], "unknown")
        self.assertEqual(self.refreshed, [])

    def test_unknown_survives_restart_and_disabled_mode_without_resubmit_or_balance_guess(self):
        snapshots = self.live()
        self.response = AmbiguousOrder("remote secret")
        first = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(first["status"], "unknown")
        self.receipts()  # A matching unrelated manual movement cannot resolve an id-less timeout.
        self.ready()
        self.pair["enabled"] = False
        self.pair["margin"]["enabled"] = False
        second = MarginBalancer(self.engine).tick(self.pair, snapshots)
        self.assertEqual(second["status"], "unknown")
        self.assertTrue(second["blocks_trading"])
        self.assertEqual(len(self.transfers), 1)
        self.assertNotIn("remote secret", str(second))

    def test_accepted_requires_both_matching_income_receipts_even_when_paused(self):
        snapshots = self.live()
        self.legacy_pending(snapshots)
        self.receipts()
        self.brokers["short"].api.rows[0]["tranId"] = "other"
        self.ready()
        self.assertEqual(self.balancer.tick(self.pair, snapshots)["status"], "accepted")
        self.receipts()
        self.ready()
        self.pair["enabled"] = False
        self.pair["margin"]["enabled"] = False
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "confirmed")
        self.assertTrue(result["blocks_trading"])
        self.assertIsNone(self.state()["pending"])
        self.assertEqual(self.state()["last_transfer"]["transaction_id"], "123")

    def test_receipt_mismatched_type_or_truncation_does_not_confirm(self):
        snapshots = self.live()
        self.legacy_pending(snapshots)
        self.receipts()
        self.brokers["short"].api.rows[0]["incomeType"] = "TRANSFER"
        self.ready()
        self.assertEqual(self.balancer.tick(self.pair, snapshots)["status"], "accepted")
        self.receipts()
        self.brokers["short"].api.rows *= 1000
        self.ready()
        self.assertTrue(self.balancer.tick(self.pair, snapshots)["blocks_trading"])
        self.assertIsNotNone(self.state()["pending"])

    def test_known_transaction_id_must_match(self):
        snapshots = self.live()
        self.response["tranId"] = "known"
        self.legacy_pending(snapshots, status="unknown")
        self.receipts(txn="other")
        self.ready()
        self.assertEqual(self.balancer.tick(self.pair, snapshots)["status"], "unknown")
        self.receipts(txn="known")
        self.ready()
        self.assertEqual(self.balancer.tick(self.pair, snapshots)["status"], "confirmed")

    def test_changed_identity_keeps_pending_record_blocked(self):
        snapshots = self.live()
        self.refresh_error = ExchangeError("test balance refresh failure")
        self.balancer.tick(self.pair, snapshots)
        self.creds["ASTER_SHORT"]["user"] = "0x" + "9" * 40
        self.ready()
        result = self.balancer.tick(self.pair, snapshots)
        self.assertTrue(result["blocks_trading"])
        self.assertIn("身份不一致", result["reason"])
        self.assertEqual(len(self.transfers), 1)

    def test_explicit_not_sent_releases_pending_but_cools_down(self):
        snapshots = self.live()
        self.response = RequestNotSent("test admission")
        self.assertEqual(self.balancer.tick(self.pair, snapshots)["status"], "rejected")
        self.assertIsNone(self.state()["pending"])
        self.assertEqual(self.balancer.tick(self.pair, snapshots)["status"], "cooldown")
        self.assertEqual(len(self.transfers), 1)

    def test_disk_failure_before_submit_never_sends(self):
        snapshots = self.live()
        with patch.object(self.store, "put", side_effect=OSError("disk full")):
            result = self.balancer.tick(self.pair, snapshots)
        self.assertTrue(result["blocks_trading"])
        self.assertEqual(self.transfers, [])

    def test_missing_usd1_cap_does_not_fallback_to_usdt(self):
        snapshots = self.live()
        self.brokers["long"].api.account["assets"][0].pop("maxWithdrawAmount")
        self.brokers["long"].api.account["maxWithdrawAmount"] = "999999"
        self.assertEqual(self.balancer.tick(self.pair, snapshots)["status"], "blocked")
        self.assertEqual(self.transfers, [])

    def test_live_gate_blocks_transfer_before_requests(self):
        snapshots = self.live()
        self.engine.live_allowed = lambda member: False
        self.assertEqual(self.balancer.tick(self.pair, snapshots)["status"], "blocked")
        self.assertEqual(self.master_calls, [])

    def test_boolean_success_code_is_not_treated_as_explicit_acceptance(self):
        snapshots = self.live()
        self.response = {"code": False}
        self.assertEqual(self.balancer.tick(self.pair, snapshots)["status"], "unknown")

    def test_failure_saving_acceptance_keeps_durable_uncertainty_without_second_submit(self):
        snapshots = self.live()
        original = self.store.put

        def fail_acceptance(key, value):
            if key == "pair_margin:gold" and (value.get("pending") or {}).get("status") == "acknowledged":
                raise OSError("test disk failure after network success")
            return original(key, value)

        with patch.object(self.store, "put", side_effect=fail_acceptance):
            self.assertTrue(self.balancer.tick(self.pair, snapshots)["blocks_trading"])
        self.assertEqual(self.state()["pending"]["status"], "submitting")
        self.ready()
        result = MarginBalancer(self.engine).tick(self.pair, snapshots)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(len(self.transfers), 1)

    def test_corrupt_or_unreadable_journal_blocks_without_submission(self):
        for malformed in ({"pending": {}}, {"pending": "invalid"}, {"next_check_at": float("nan")}):
            self.store.put("pair_margin:gold", malformed)
            self.assertTrue(self.balancer.tick(self.pair, self.snapshots())["blocks_trading"])
        with patch.object(self.store, "get", side_effect=OSError("read failed")):
            self.assertTrue(self.balancer.tick(self.pair, {})["blocks_trading"])

    def test_external_open_order_is_checked_before_live_transfer(self):
        snapshots = self.live()
        original = self.brokers["short"].api.call

        def with_external(method, path, *args, **kwargs):
            return [{"orderId": "external"}] if path.endswith("/openOrders") else original(method, path, *args, **kwargs)

        self.brokers["short"].api.call = with_external
        snapshots = {side: replace(value, open_orders=None) for side, value in snapshots.items()}
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.transfers, [])

    def test_fresh_balance_movement_uses_lower_equity_and_new_available(self):
        snapshots = self.live()
        self.brokers["long"].api.account["assets"][0].update(availableBalance="2000", marginBalance="2000")
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "acknowledged")
        self.assertEqual(result["plan"]["amount"], "500.00000000")

    def test_no_new_http_reads_until_check_interval_after_failed_ownership(self):
        snapshots = self.live()
        self.listing[2]["sourceAddr"] = "0x" + "9" * 40
        self.assertEqual(self.balancer.tick(self.pair, snapshots)["status"], "blocked")
        count = len(self.master_calls)
        self.assertEqual(self.balancer.tick(self.pair, snapshots)["status"], "blocked")
        self.assertEqual(len(self.master_calls), count)

    def test_revoked_original_snapshot_aborts_before_live_reads(self):
        snapshots = self.live()
        self.brokers["long"]._cycle_account_event("ACCOUNT_UPDATE")
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("失效", result["reason"])
        self.assertEqual(self.master_calls, [])
        self.assertEqual(self.transfers, [])

    def test_account_events_during_each_http_stage_prevent_transfer(self):
        for side in ("long", "short"):
            for endpoint in ("accountWithJoinMargin", "income", "openOrders"):
                with self.subTest(side=side, endpoint=endpoint):
                    snapshots = self.live()
                    self.store.put("pair_margin:gold", {})
                    original = self.brokers[side].api.call

                    def changed(method, path, *args, **kwargs):
                        response = original(method, path, *args, **kwargs)
                        if path.endswith("/" + endpoint):
                            self.brokers[side]._cycle_account_event("ACCOUNT_UPDATE")
                        return response

                    with patch.object(self.brokers[side].api, "call", side_effect=changed):
                        result = self.balancer.tick(self.pair, snapshots)
                    self.assertEqual(result["status"], "blocked")
                    self.assertEqual(self.transfers, [])
                    self.assertIsNone(self.state().get("pending"))

    def test_event_after_intent_commit_is_definitively_not_sent(self):
        snapshots = self.live()
        original = self.store.put

        def commit_then_event(key, value):
            original(key, value)
            if key == "pair_margin:gold" and (value.get("pending") or {}).get("status") == "submitting":
                self.brokers["short"]._cycle_account_event("ORDER_TRADE_UPDATE")

        with patch.object(self.store, "put", side_effect=commit_then_event):
            result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "rejected")
        self.assertIsNone(self.state()["pending"])
        self.assertEqual(self.transfers, [])

    def test_event_during_real_signing_is_checked_before_http_transport(self):
        snapshots = self.live()
        sent = []
        signed = []

        def factory(credentials, *, budget, before_submit):
            api = TransferAPI(credentials, budget=budget, before_submit=before_submit,
                transport=httpx.MockTransport(lambda request: sent.append(request) or
                    httpx.Response(200, json={"code": 200, "msg": "success"})))
            original = api.signed_parameters

            def sign_then_event(params):
                result = original(params)
                signed.append(True)
                self.brokers["long"]._cycle_account_event("ACCOUNT_UPDATE")
                return result

            api.signed_parameters = sign_then_event
            return api

        with patch("trading.margin_balance.TransferAPI", side_effect=factory):
            result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(signed, [True])
        self.assertEqual(sent, [])
        self.assertEqual(result["status"], "rejected")
        self.assertIsNone(self.state()["pending"])

    def test_hot_snapshot_without_generation_gets_own_current_read(self):
        snapshots = self.live()
        for side in ("long", "short"):
            del snapshots[side].account_read_generation
            self.brokers[side]._cycle_account_event("ACCOUNT_UPDATE")
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "acknowledged")
        self.assertCountEqual(self.refreshed[:2], ["long", "short"])
        self.assertEqual(self.refreshed[2:], ["long", "short"])
        self.assertEqual(len(self.transfers), 1)

    def test_hot_snapshot_reads_are_parallel_and_keep_event_revocation(self):
        snapshots = self.live()
        barrier = threading.Barrier(2, timeout=3)
        modes = []
        for side, broker in self.brokers.items():
            del snapshots[side].account_read_generation
            original = broker.snapshot

            def fresh(symbols, fresh_modes=False, side=side, original=original):
                modes.append((side, fresh_modes))
                current = original(symbols, fresh_modes=fresh_modes)
                barrier.wait()
                if side == "short":
                    self.brokers["long"]._cycle_account_event("ACCOUNT_UPDATE")
                return current

            broker.snapshot = fresh
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "blocked")
        self.assertCountEqual(modes, [("long", False), ("short", False)])
        self.assertEqual(self.transfers, [])

    def test_hot_margin_polling_reuses_mode_ttl_within_default_budget(self):
        self.live()
        now = [1000.0]
        calls = []
        with patch("time.time", side_effect=lambda: now[0]), patch("time.monotonic", side_effect=lambda: now[0]):
            budget = RateBudget()
            for broker in self.brokers.values():
                broker.snapshot = LiveBroker.snapshot.__get__(broker, LiveBroker)
                broker.api.budget = budget
                broker.api.account["assets"][0].update(availableBalance="2000", marginBalance="2000",
                    maxWithdrawAmount="2000", crossWalletBalance="2000", crossUnPnl="0")
                original = broker.api.call

                def call(method, path, params=None, original=original, **kwargs):
                    weight = kwargs.get("weight", 1)
                    budget.reserve(weight)
                    calls.append((path, weight))
                    if path.endswith("/positionSide/dual"):
                        return {"dualSidePosition": True}
                    if path.endswith("/multiAssetsMargin"):
                        return {"multiAssetsMargin": False}
                    if path.endswith("/positionRisk"):
                        return []
                    if path.endswith("/leverageBracket"):
                        return {"symbol": "XAUUSD1", "brackets": [{"notionalFloor": "0", "notionalCap": "10000000",
                            "maintMarginRatio": "0.005", "cum": "0", "initialLeverage": 125}]}
                    return original(method, path, params, **kwargs)

                broker.api.call = call

            def master_call(method, path, **kwargs):
                budget.reserve(kwargs["weight"])
                calls.append((path, kwargs["weight"]))
                return copy.deepcopy(self.listing)

            master = SimpleNamespace(budget=budget, call=master_call, close=lambda: None)
            with patch("trading.margin_balance.API", return_value=master):
                for _ in range(12):
                    result = self.balancer.tick(self.pair, {"long": snapshot(2000), "short": snapshot(2000)})
                    self.assertEqual(result["status"], "waiting", result)
                    now[0] += 5
            self.assertEqual(sum(weight for _, weight in calls), 924)
            self.assertEqual(sum(path.endswith("/positionSide/dual") for path, _ in calls), 8)
            self.assertEqual(sum(path.endswith("/multiAssetsMargin") for path, _ in calls), 8)
            self.assertEqual(self.transfers, [])

            # A mode-changing event evicts the reused modes before the next read.
            self.brokers["long"]._cycle_account_event("ACCOUNT_CONFIG_UPDATE")
            self.assertNotIn("dual", self.brokers["long"].cached_at)
            self.assertNotIn("multi", self.brokers["long"].cached_at)

    def test_generationless_replacement_read_cannot_authorize_transfer(self):
        snapshots = self.live()
        del snapshots["long"].account_read_generation
        self.brokers["long"].snapshot = lambda *args, **kwargs: snapshot(3000)
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(self.transfers, [])

    def test_new_account_positions_cannot_be_ignored_without_an_event(self):
        snapshots = self.live()
        self.brokers["long"].api.account["positions"] = [{"symbol": "XAUUSD1", "positionSide": "LONG",
            "positionAmt": "10", "leverage": "5", "isolated": False, "positionInitialMargin": "2000"}]
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "blocked")
        self.assertIn("持仓", result["reason"])
        self.assertEqual(self.transfers, [])

    def test_changed_or_missing_position_fields_never_authorize_transfer(self):
        snapshots = self.live()
        snapshots["long"].positions = [Position("XAUUSD1", "LONG", dec(1), dec(100), dec(100), 5)]
        valid = {"symbol": "XAUUSD1", "positionSide": "LONG", "positionAmt": "1", "leverage": "5", "isolated": False}
        cases = [[{**valid, "positionAmt": "2"}], [{**valid, "leverage": "10"}], [{**valid, "isolated": True}], [], None]
        cases.extend([[{key: value for key, value in valid.items() if key != missing}]
                      for missing in ("symbol", "positionSide", "positionAmt", "leverage", "isolated")])
        for rows in cases:
            with self.subTest(rows=rows):
                self.ready()
                self.brokers["long"].api.account["positions"] = rows
                result = self.balancer.tick(self.pair, snapshots)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(self.transfers, [])

    def test_newer_exchange_occupation_limits_transfer_despite_unchanged_quantity(self):
        for source in ("positionInitialMargin", "initialMargin", "markPrice", "asset"):
            with self.subTest(source=source):
                snapshots = self.live()
                self.store.put("pair_margin:gold", {})
                snapshots["long"].positions = [Position("XAUUSD1", "LONG", dec(1), dec(100), dec(100), 5)]
                row = {"symbol": "XAUUSD1", "positionSide": "LONG", "positionAmt": "1", "leverage": "5", "isolated": False}
                if source == "asset":
                    self.brokers["long"].api.account["assets"][0]["positionInitialMargin"] = "1125"
                else:
                    row[source] = "5625" if source == "markPrice" else "1125"
                self.brokers["long"].api.account["positions"] = [row]
                result = self.balancer.tick(self.pair, snapshots)
                self.assertEqual(result["status"], "acknowledged")
                self.assertEqual(result["plan"]["amount"], "500.00000000")
                self.assertEqual(len(self.transfers), 1)


class TransferSigningTests(unittest.TestCase):
    def test_exact_form_has_signer_only_and_uses_approved_agent_key(self):
        key = bytes(range(1, 33))
        credentials = {"private_key": key, "signer": Account.from_key(key).address, "user": "0x" + "22" * 20}
        requests = []
        api = TransferAPI(credentials, budget=RateBudget(), transport=httpx.MockTransport(
            lambda request: requests.append(request) or httpx.Response(200, json={"code": 200})))
        self.addCleanup(api.close)
        api.call("POST", "/fapi/v3/subAccountTransfer", {"asset": "USD1", "amount": "1.00000000",
            "kindType": "FUTURE_FUTURE", "fromAccountAddress": "0x" + "33" * 20,
            "toAccountAddress": "0x" + "44" * 20}, signed=True, weight=5)
        data = dict(parse_qsl(requests[0].content.decode()))
        signature = data.pop("signature")
        self.assertNotIn("user", data)
        message = encode_typed_data(domain_data={"name": "AsterSignTransaction", "version": "1", "chainId": 1666,
            "verifyingContract": "0x" + "00" * 20}, message_types={"Message": [{"name": "msg", "type": "string"}]},
            message_data={"msg": urlencode(data)})
        self.assertEqual(Account.recover_message(message, signature=signature), credentials["signer"])
        self.assertEqual(requests[0].url.query, b"")
        with self.assertRaises(RequestNotSent):
            api.signed_parameters({"user": credentials["user"]})
