"""Count real broker endpoints while funds reconciliation owns the pair."""
from contextlib import contextmanager
from copy import deepcopy
import time
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_budget as fixtures
from trading.exchange import AmbiguousOrder, ExchangeError
from trading.margin_balance import MarginBalancer
from trading.models import TradingError, dec
from trading.pair_execution import runtime_default


ACCOUNT = "/fapi/v3/accountWithJoinMargin"
INCOME = "/fapi/v3/income"


class PairMarginPollEfficiencyTests(TestCase):
    live_brokers = fixtures.PairBudgetTests.live_brokers

    def setUp(self):
        fixtures.PairBudgetTests.setUp(self)
        self.margin.stop()
        self.live_brokers()
        self.wall, self.ticks = time.time(), time.monotonic()
        for name, value in (("time.time", lambda: self.wall), ("time.monotonic", lambda: self.ticks)):
            clock = patch(name, side_effect=value)
            clock.start()
            self.addCleanup(clock.stop)
        for side in self.brokers:
            self.store.save_account({**self.store.account(side), "mode": "live"})
        self.engine.live_allowed = lambda account: True
        self.pair["cycle"]["enabled"] = False
        self.pair["ordinary"]["enabled"] = True
        self.pair["margin"].update(enabled=True, master_env_prefix="ASTER_MASTER")
        self.pair = self.store.save_pair(self.pair)
        self.engine.capacities = lambda symbol: {5: dec(0), 10: dec(0), 20: dec(0)}
        self.children = {side: broker.api.credentials for side, broker in self.brokers.items()}
        self.master = {"user": "0x" + "f" * 40}
        self.income_rows = {side: [] for side in self.brokers}

        @contextmanager
        def master_context(balancer, pair, members):
            # No credentials or transports are opened by these offline tests.
            yield None, self.master, self.children

        master = patch.object(MarginBalancer, "_master", master_context)
        master.start()
        self.addCleanup(master.stop)
        for side, broker in self.brokers.items():
            original = broker.api.call

            def call(method, path, params=None, *, signed=False, weight=1, timeout=None, side=side, original=original):
                if path == INCOME:
                    api = self.brokers[side].api
                    api.budget.reserve(weight)
                    api.calls.append((method, path, api.budget._priority_flags()))
                    return deepcopy(self.income_rows[side])
                return original(method, path, params, signed=signed, weight=weight, timeout=timeout)

            broker.api.call = call

    def advance(self, seconds):
        self.wall += seconds
        self.ticks += seconds

    def calls(self, path=None):
        return [row for broker in self.brokers.values() for row in broker.api.calls
                if path is None or row[1] == path]

    def assert_read_only(self):
        self.assertTrue(all(method == "GET" for method, _, _ in self.calls()))

    def pending(self, status="accepted", *, wait=0, transaction=True):
        members = {side: self.store.account(side) for side in self.brokers}
        record = {"request_id": "offline-transfer", "source": "long", "destination": "short",
                  "amount": "10", "created_at": self.wall - 10, "status": status,
                  "identity": MarginBalancer._fingerprint(self.pair, members, self.children, self.master),
                  "before_receipts": {"long": [], "short": []}}
        if transaction:
            record["transaction_id"] = "123"
        if status == "acknowledged":
            record["acknowledged_at"] = self.wall - 1
        self.store.put("pair_margin:gold", {"pending": record, "next_check_at": self.wall + wait})
        return record

    def tick(self):
        return self.trader.tick(self.pair)

    def test_pending_before_deadline_has_no_account_or_income_calls(self):
        self.pending(wait=5)
        for _ in range(5):
            state = self.tick()
            self.assertEqual(state["phase"], "margin_wait")
            self.assertTrue(state["margin"]["blocks_trading"])
            self.advance(1)
        self.assertEqual(self.calls(), [])
        self.tick()
        self.assertEqual(len(self.calls(INCOME)), 2)
        self.assertEqual(self.calls(ACCOUNT), [])
        self.assertEqual(self.budget.local_weight, 60)
        self.assert_read_only()

    def test_paused_unknown_reads_balances_once_then_skips_without_guessing_confirmation(self):
        self.pending("unknown", transaction=False)
        self.pair = self.store.save_pair({**self.pair, "enabled": False})
        for index in range(12):
            state = self.tick()
            self.assertEqual(state["margin"]["status"], "skipped" if index == 0 else "paused")
            self.assertEqual(state["margin"]["blocks_trading"], index == 0)
            self.assertEqual(state["phase"], "margin_wait" if index == 0 else "paused")
            self.advance(5)
        self.assertEqual(len(self.calls(ACCOUNT)), 2)
        self.assertEqual(self.calls(INCOME), [])
        self.assertIn("long", state["snapshots"])
        self.assert_read_only()
        journal = self.store.get("pair_margin:gold")
        self.assertIsNone(journal["pending"])
        self.assertEqual(journal["last_transfer"]["status"], "skipped")
        self.assertNotIn("confirmed_at", journal["last_transfer"])

    def test_reconciliation_completion_ends_turn_and_next_turn_reads_both_accounts(self):
        self.tick()  # Seed an ordinary negative observation that must be discarded.
        before = len(self.calls(ACCOUNT))
        record = self.pending()
        for side, sign in (("long", -1), ("short", 1)):
            self.income_rows[side] = [{"incomeType": "SUBUSER_ASSET_TRANSFER", "asset": "USD1",
                "income": str(10 * sign), "tranId": "123", "time": int(record["created_at"] * 1000)}]
        with patch.object(self.trader, "_start", side_effect=AssertionError("no orders on reconciliation turn")):
            state = self.tick()
        self.assertEqual(state["margin"]["status"], "confirmed")
        self.assertTrue(state["margin"]["blocks_trading"])
        self.assertIsNone(self.store.get("pair_margin:gold")["pending"])
        self.assertEqual(len(self.calls(ACCOUNT)), before)
        self.assertEqual(len(self.calls(INCOME)), 2)
        self.tick()
        self.assertEqual(len(self.calls(ACCOUNT)), before + 2)
        self.assert_read_only()

    def test_acknowledged_reads_each_account_once_then_requires_another_trading_read(self):
        self.pending("acknowledged")
        state = self.tick()
        self.assertEqual(state["margin"]["status"], "acknowledged")
        self.assertEqual(len(self.calls(ACCOUNT)), 2)
        self.assertEqual(self.calls(INCOME), [])
        self.assertIsNone(self.store.get("pair_margin:gold")["pending"])
        self.tick()
        self.assertEqual(len(self.calls(ACCOUNT)), 4)
        self.assert_read_only()

    def monitor_wait(self, *, cooldown=False):
        self.pair["ordinary"]["enabled"] = False
        self.pair = self.store.save_pair(self.pair)
        self.store.put("pair_margin:gold", {
            "cooldown_until" if cooldown else "next_check_at": self.wall + 120})
        self.assertEqual(self.tick()["phase"], "monitoring")
        self.assertEqual(len(self.calls(ACCOUNT)), 2)

    def test_monitor_only_retains_thirty_second_display_refresh_during_wait_and_cooldown(self):
        for cooldown in (False, True):
            with self.subTest(cooldown=cooldown):
                for broker in self.brokers.values():
                    broker.api.calls.clear()
                self.monitor_wait(cooldown=cooldown)
                for _ in range(5):
                    self.advance(5)
                    self.assertEqual(self.tick()["phase"], "monitoring")
                self.assertEqual(len(self.calls(ACCOUNT)), 2)
                self.advance(5)
                self.tick()
                self.assertEqual(len(self.calls(ACCOUNT)), 4)
                self.assert_read_only()

    def test_due_check_does_not_use_display_observation_as_transfer_authority(self):
        self.monitor_wait()
        self.store.put("pair_margin:gold", {"next_check_at": self.wall + 1})
        self.advance(1)
        state = self.tick()
        self.assertEqual(len(self.calls(ACCOUNT)), 4)
        self.assertFalse(state["margin"]["blocks_trading"])
        self.assert_read_only()

    def test_mode_revision_and_account_event_force_new_read_before_waiting(self):
        self.monitor_wait()
        self.brokers["long"]._cycle_account_event("ACCOUNT_UPDATE")
        self.tick()
        self.assertEqual(len(self.calls(ACCOUNT)), 4)
        self.pair = self.store.save_pair({**self.pair, "name": "updated monitoring"})
        self.tick()
        self.assertEqual(len(self.calls(ACCOUNT)), 6)
        self.pair["ordinary"]["enabled"] = True
        self.pair = self.store.save_pair(self.pair)
        self.tick()
        self.assertEqual(len(self.calls(ACCOUNT)), 8)
        self.assert_read_only()

    def test_changed_identity_and_attention_remain_visible(self):
        self.monitor_wait()
        self.brokers["long"].api.credentials = {**self.children["long"], "user": "0x" + "e" * 40}
        state = self.tick()
        self.assertEqual(state["phase"], "attention")
        self.assertIn("真实账户", state["reason"])
        self.assertEqual(len(self.calls(ACCOUNT)), 2)
        self.assert_read_only()

    def test_pending_identity_mismatch_blocks_without_querying_another_account(self):
        record = self.pending()
        record["identity"] = "different-binding"
        self.store.put("pair_margin:gold", {"pending": record})
        state = self.tick()
        self.assertEqual(state["phase"], "margin_wait")
        self.assertTrue(state["margin"]["blocks_trading"])
        self.assertIn("身份不一致", state["reason"])
        self.assertEqual(self.calls(), [])

    def test_event_after_read_cannot_extend_display_wait_with_old_positions(self):
        self.pair["ordinary"]["enabled"] = False
        self.pair = self.store.save_pair(self.pair)
        self.store.put("pair_margin:gold", {"next_check_at": self.wall + 120})
        publish = self.trader._publish_snapshots

        def account_changes_after_read(state, snapshots):
            publish(state, snapshots)
            self.brokers["long"]._cycle_account_event("ACCOUNT_UPDATE")

        with patch.object(self.trader, "_publish_snapshots", side_effect=account_changes_after_read):
            self.tick()
        self.tick()
        self.assertEqual(len(self.calls(ACCOUNT)), 4)
        self.assert_read_only()

    def test_invalid_journal_and_nonfinite_deadlines_never_disappear_behind_wait(self):
        for journal in ([], {"pending": {}}, {"next_check_at": float("inf")},
                        {"checked_at": "bad"}, {"cooldown_until": True}):
            with self.subTest(journal=journal):
                self.store.put("pair_margin:gold", journal)
                state = self.tick()
                self.assertEqual(state["phase"], "margin_wait")
                self.assertTrue(state["margin"]["blocks_trading"])
                self.assertIn("无效", state["reason"])
        self.assertEqual(self.calls(), [])

    def test_pending_transfer_keeps_its_read_only_priority_with_orders_and_holdings(self):
        self.pending(wait=5)
        _, _, identities = self.trader._members(self.pair)
        state = runtime_default()
        state["pending"] = {"identities": identities}
        self.store.put("pair_runtime:gold", state)
        with patch.object(self.trader, "_recover") as recover:
            result = self.tick()
        recover.assert_called_once()
        self.assertEqual(result["margin"]["status"], "accepted")
        self.assertTrue(result["margin"]["blocks_trading"])
        state.update(pending=None)
        state["progress"].update(quantities={"LONG": "1", "SHORT": "1"},
                                 opened_at=self.wall - 10, config={"hold_seconds": 1})
        self.store.put("pair_runtime:gold", state)
        with patch.object(self.trader, "_read", side_effect=TradingError("read due reduction")) as read:
            result = self.tick()
        self.assertEqual(result["margin"]["status"], "accepted")
        self.assertEqual(result["reason"], "read due reduction")
        self.assertTrue(read.call_args.kwargs["reconciliation"])
        self.assertEqual(self.calls(), [])

    def test_unknown_and_accepted_transfers_do_not_freeze_due_reduction(self):
        for status in ("unknown", "accepted", "acknowledged"):
            with self.subTest(status=status):
                for side, broker in self.paper.items():
                    broker.state["positions"]["XAUUSD1:" + side.upper()].update(qty="0.01", entry="4412.015")
                    broker.save()
                state = runtime_default()
                state["progress"].update(phase="holding", quantities={"LONG": "0.01", "SHORT": "0.01"},
                    opened_at=self.wall - 10, config={**self.pair["cycle"], "enabled": True,
                                                    "hold_seconds": 1, "leverage": 5})
                self.store.put("pair_runtime:gold", state)
                record = self.pending(status, transaction=status != "unknown")
                result = self.tick()
                self.assertIsNone(result["pending"], result)
                self.assertEqual(result["progress"]["completed_cycles"], 1, result)
                self.assertEqual(result["margin"]["status"], "skipped" if status == "unknown" else status)
                for side, broker in self.paper.items():
                    self.assertEqual(dec(broker.state["positions"]["XAUUSD1:" + side.upper()]["qty"]), 0)
                stored = self.store.get("pair_margin:gold")["pending"]
                self.assertEqual(stored, record if status == "accepted" else None)
                if status == "unknown":
                    self.assertEqual(self.store.get("pair_margin:gold")["last_transfer"]["request_id"], record["request_id"])
                self.advance(5)

    def test_unknown_transfer_does_not_starve_pending_close_order_recovery(self):
        for side, broker in self.paper.items():
            broker.state["positions"]["XAUUSD1:" + side.upper()].update(qty="0.01", entry="4412.015")
            broker.save()
        state = runtime_default()
        state["progress"].update(phase="holding", quantities={"LONG": "0.01", "SHORT": "0.01"},
            opened_at=self.wall - 10, config={**self.pair["cycle"], "enabled": True,
                                            "hold_seconds": 1, "leverage": 5})
        self.store.put("pair_runtime:gold", state)
        transfer = self.pending("unknown", transaction=False)
        long = self.brokers["long"]
        submit = long.submit

        def uncertain(orders, **options):
            submit(orders, **options)
            raise AmbiguousOrder("lost close receipt")

        with patch.object(long, "submit", side_effect=uncertain), \
             patch.object(long, "query", side_effect=ExchangeError("not found yet", code=-2013)):
            result = self.tick()
        self.assertIsNotNone(result["pending"], result)
        self.assertEqual(result["margin"]["status"], "skipped")
        self.assertIsNone(self.store.get("pair_margin:gold")["pending"])
        order_id = result["pending"]["legs"][0]["order"]["newClientOrderId"]
        self.advance(5)
        with patch.object(long, "query", wraps=long.query) as query, \
             patch.object(long, "submit", side_effect=AssertionError("never repeat close")), \
             patch.object(self.brokers["short"], "submit", side_effect=AssertionError("never repeat close")), \
             patch.object(MarginBalancer, "_live", side_effect=AssertionError("no new transfer")):
            result = self.tick()
        query.assert_called_once_with("XAUUSD1", order_id)
        self.assertIsNone(result["pending"], result)
        self.assertEqual(result["progress"]["completed_cycles"], 1)
        self.assertEqual(result["margin"]["status"], "waiting")
        self.assertFalse(result["margin"]["blocks_trading"])
        journal = self.store.get("pair_margin:gold")
        self.assertIsNone(journal["pending"])
        self.assertEqual(journal["last_transfer"]["status"], "skipped")
        self.assertEqual(journal["last_transfer"]["request_id"], transfer["request_id"])
        for side, broker in self.paper.items():
            self.assertEqual(len(broker.state["orders"]), 1)
            self.assertEqual(dec(broker.state["positions"]["XAUUSD1:" + side.upper()]["qty"]), 0)
