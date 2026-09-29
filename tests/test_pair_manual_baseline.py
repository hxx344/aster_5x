"""Manual baseline adoption uses real paper fills and offline live GET fixtures."""
from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timezone
import sqlite3
import time
import unittest
from unittest.mock import patch

from tests import test_pair_integration as integration
from tests import test_pair_order_recovery as recovery
from tests.test_pair_notional_rejection import ORIGINAL_REJECTION
from trading.engine import Engine
from trading.exchange import BudgetWait, ExchangeError
from trading.execution import Executor
from trading.models import TradingError, dec, wire
from trading.store import Store


SYMBOL = recovery.SYMBOL
BEFORE = {"LONG": "0.3", "SHORT": "0.7"}
QUANTITY = "1.209"


class PairManualBaselineTests(unittest.TestCase):
    setUp = integration.PairIntegrationTests.setUp
    state = recovery.PairOrderRecoveryTests.state
    broker = recovery.PairOrderRecoveryTests.broker
    change_state = recovery.PairOrderRecoveryTests.change_state
    no_writes = recovery.PairOrderRecoveryTests.no_writes

    def seed(self, filled=("short",)):
        original = recovery.seed_pending(self.engine, BEFORE)
        for side in ("long", "short"):
            self.broker(side).state["orders"] = {}
            self.broker(side).save()
        pending = original["pending"]
        pending.update(quantity=QUANTITY, created_at=time.time() - 1,
                       target={side: wire(dec(qty) + dec(QUANTITY)) for side, qty in BEFORE.items()})
        for leg in pending["legs"]:
            leg["order"]["quantity"] = QUANTITY
            leg["submit_evidence_version"] = 1
            if leg["key"] in filled:
                leg["receipt"] = self.broker(leg["key"]).submit([leg["order"]])[0]
                self.assertEqual(leg["receipt"]["status"], "FILLED")
            else:
                leg["receipt"] = {**recovery.receipt_for(leg, status="REJECTED"),
                                  "reject_code": -2029, "reject_reason": ORIGINAL_REJECTION}
            leg["error"] = None
        self.f.store.put("pair_runtime:gold", original)
        return deepcopy(original)

    def reduce(self, side, quantity=QUANTITY, *, client_id=None):
        order = Executor.order(SYMBOL, side.upper(), "SELL" if side == "long" else "BUY",
                               dec(quantity), client_id or "external-manual-" + side)
        receipt = self.broker(side).submit([order])[0]
        self.assertEqual(receipt["status"], "FILLED")
        return receipt

    def check(self):
        with self.no_writes():
            return self.engine.pairs.check_recovery("gold")

    def assert_retained(self, original):
        current = self.state()
        self.assertIsNotNone(current["pending"])
        self.assertEqual(current["pending"]["id"], original["pending"]["id"])
        self.assertEqual(current["owned"], original["owned"])
        self.assertEqual(current["daily_volume"], original["daily_volume"])
        self.assertIsNone(self.f.store.get("pair_batch:" + original["pending"]["id"]))

    def assert_blocked(self, original):
        try:
            result = self.check()
        except TradingError:
            pass
        else:
            self.assertFalse(result["completed"], result)
        self.assert_retained(original)

    def assert_completed_at_baseline(self, original, external):
        result = self.check()
        self.assertTrue(result["completed"], result)
        self.assertFalse(result["archive_available"])
        state = self.state()
        self.assertIsNone(state["pending"])
        self.assertFalse(state["last_batch"]["completed"])
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertEqual(state["owned"], original["owned"])
        self.assertEqual(state["progress"], original["progress"])
        batch = self.f.store.get("pair_batch:" + original["pending"]["id"])
        self.assertEqual(batch["legs"], original["pending"]["legs"])
        self.assertEqual(batch["repairs"], original["pending"]["repairs"])
        audit = batch["manual_position_reconciliation"]
        self.assertEqual(audit["before"], original["owned"])
        self.assertEqual(audit["actual"], original["owned"])
        self.assertEqual({side: dec(qty) for side, qty in audit["external_reduction"].items()}, external)
        self.assertEqual({side: dec(qty) for side, qty in audit["known_expected"].items()},
                         {side: dec(BEFORE[side]) + external[side] for side in BEFORE})
        self.assertGreater(audit["checked_at"], 0)
        self.assertLessEqual(audit["checked_at"], time.time())
        self.assertIsInstance(audit["source"], str)
        self.assertTrue(audit["source"])
        expected_volume = deepcopy(original["daily_volume"])
        for leg in original["pending"]["legs"] + original["pending"]["repairs"]:
            receipt = leg["receipt"]
            if not dec(receipt["executedQty"]):
                continue
            date = datetime.fromtimestamp(receipt["updateTime"] / 1000, timezone.utc).date().isoformat()
            row = expected_volume.setdefault(date, {"long": "0", "short": "0"})
            row[leg["key"]] = wire(dec(row[leg["key"]]) + dec(receipt["executedQty"]) * dec(receipt["avgPrice"]))
        self.assertEqual(state["daily_volume"], expected_volume)
        return state, batch

    def test_single_filled_leg_can_be_manually_reduced_to_its_own_baseline(self):
        original = self.seed()
        external = self.reduce("short")
        # Autonomous recovery must not adopt the external position change.
        with self.no_writes():
            self.engine.pairs.tick("gold")
        self.assert_retained(original)
        state, batch = self.assert_completed_at_baseline(original, {"LONG": dec(0), "SHORT": dec(QUANTITY)})
        self.assertNotIn(external["clientOrderId"], str(batch))
        with self.assertRaisesRegex(TradingError, "当前没有待核对批次"):
            self.check()
        self.assertEqual(self.state()["daily_volume"], state["daily_volume"])
        restarted = Engine(Store(self.f.store.path), market=self.f.market)
        self.addCleanup(restarted.dashboard_reports.close)
        with patch("trading.paper.PaperBroker.submit", side_effect=AssertionError("must not reduce again")):
            restarted.pairs.tick("gold")
            with self.assertRaises(TradingError):
                restarted.pairs.check_recovery("gold")
        self.assertEqual(self.state()["daily_volume"], state["daily_volume"])
        self.assertEqual(self.f.store.get("pair_batch:" + batch["id"]), batch)

    def test_both_filled_legs_manual_reduction_preserves_unequal_baselines(self):
        original = self.seed(("long", "short"))
        for side in ("long", "short"):
            self.reduce(side)
        self.assert_completed_at_baseline(original, {"LONG": dec(QUANTITY), "SHORT": dec(QUANTITY)})

    def test_known_partial_repair_is_preserved_and_only_remainder_is_external(self):
        self.seed()
        order = Executor.order(SYMBOL, "SHORT", "BUY", dec("0.209"), "known-repair")
        receipt = self.broker("short").submit([order])[0]
        original = self.change_state(lambda state: state["pending"].update(repair_attempts=1, repairs=[
            {"key": "short", "order": order, "receipt": receipt, "dispatch": "sending"}]))
        self.reduce("short", "1")
        self.assert_completed_at_baseline(original, {"LONG": dec(0), "SHORT": dec(1)})

    def test_unknown_original_order_and_query_budget_wait_keep_pending(self):
        for error in (ExchangeError("order absent", code=-2013),
                      BudgetWait("本地 API 请求权重预算不足", retry_after=30)):
            with self.subTest(error=type(error).__name__):
                original = self.seed()
                self.reduce("short", client_id="external-" + type(error).__name__)
                self.change_state(lambda state: state["pending"]["legs"][0].update(receipt=None))
                with patch.object(self.broker(), "query", side_effect=error):
                    self.assert_blocked(original)

    def test_unknown_repair_cannot_be_replaced_by_observed_baseline(self):
        self.seed()
        self.reduce("short")
        order = Executor.order(SYMBOL, "SHORT", "BUY", dec(QUANTITY), "unknown-repair")
        original = self.change_state(lambda state: state["pending"].update(repair_attempts=1, repairs=[
            {"key": "short", "order": order, "receipt": None, "dispatch": "sending"}]))
        with patch.object(self.broker("short"), "query", side_effect=ExchangeError("order absent", code=-2013)):
            self.assert_blocked(original)

    def test_partial_manual_reduction_does_not_finish(self):
        original = self.seed()
        self.reduce("short", "1")
        self.assert_blocked(original)

    def test_manual_over_reduction_does_not_adopt_a_smaller_baseline(self):
        original = self.seed()
        self.reduce("short", "1.309")
        self.assert_blocked(original)

    def test_known_repairs_below_baseline_cannot_be_hidden_by_external_reopening(self):
        self.seed()
        repairs = []
        for index in range(2):
            order = Executor.order(SYMBOL, "SHORT", "BUY", dec("0.7"), f"known-repair-{index}")
            receipt = self.broker("short").submit([order])[0]
            self.assertEqual(receipt["status"], "FILLED")
            repairs.append({"key": "short", "order": order, "receipt": receipt, "dispatch": "sending"})
        original = self.change_state(lambda state: state["pending"].update(repairs=repairs, repair_attempts=2))
        self.assertEqual(self.broker("short").snapshot([SYMBOL]).pair(SYMBOL)[1].qty, dec("0.509"))
        added = self.broker("short").submit([
            Executor.order(SYMBOL, "SHORT", "SELL", dec("0.191"), "external-reopened-baseline")])[0]
        self.assertEqual(added["status"], "FILLED")
        self.assertEqual(self.broker("short").snapshot([SYMBOL]).pair(SYMBOL)[1].qty, dec(BEFORE["SHORT"]))
        self.assert_blocked(original)

    def test_equal_sides_are_not_a_substitute_for_each_saved_baseline(self):
        original = self.seed(("long", "short"))
        self.reduce("long", "1.009")
        self.reduce("short", "1.409")
        self.assertEqual(self.broker().snapshot([SYMBOL]).pair(SYMBOL)[0].qty, dec("0.5"))
        self.assertEqual(self.broker("short").snapshot([SYMBOL]).pair(SYMBOL)[1].qty, dec("0.5"))
        self.assert_blocked(original)

    def test_changed_leverage_keeps_pending_even_when_both_sides_match_baseline(self):
        original = self.seed()
        self.reduce("short")
        for side in ("long", "short"):
            self.broker(side).set_leverage(SYMBOL, 10)
        self.assert_blocked(original)

    def test_other_market_or_opposite_position_prevents_manual_adoption(self):
        for symbol, side in (("SPCXUSD1", "LONG"), (SYMBOL, "SHORT")):
            with self.subTest(symbol=symbol, side=side):
                original = self.seed()
                self.reduce("short", client_id="external-" + symbol)
                broker = self.broker()
                broker.submit([Executor.order(symbol, side, "BUY" if side == "LONG" else "SELL",
                                               dec("0.1"), "unrelated-" + symbol)])
                self.assert_blocked(original)
                broker.state["positions"][symbol + ":" + side].update(qty="0", entry="0")
                broker.save()

    def test_identity_change_or_competing_work_prevents_manual_adoption(self):
        original = self.seed()
        self.reduce("short")
        original_state = self.state()
        mutations = (
            lambda state: state["pending"]["identities"]["long"].update(account_id="other"),
            lambda state: state["progress"]["quantities"].update(LONG="0.01"),
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.f.store.put("pair_runtime:gold", deepcopy(original_state))
                self.change_state(mutation)
                self.assert_blocked(original)
        self.f.store.put("pair_runtime:gold", original_state)
        self.f.store.put("pair_margin:gold", {"pending": {"request_id": "other", "status": "unknown"}})
        self.assert_blocked(original)

    def test_batch_history_write_failure_rolls_back_completion_and_volume(self):
        original = self.seed()
        self.reduce("short")
        with self.f.store.connect() as db:
            db.execute("CREATE TRIGGER fail_manual_batch BEFORE INSERT ON kv "
                       "WHEN NEW.key='pair_batch:" + original["pending"]["id"] + "' "
                       "BEGIN SELECT RAISE(ABORT, 'manual history failure'); END")
        with self.assertRaisesRegex((sqlite3.DatabaseError, TradingError), "manual history failure"):
            self.check()
        self.assert_retained(original)


class PairManualBaselineLiveTests(unittest.TestCase):
    setUp = integration.PairSnapshotLifecycleTests.setUp
    tearDown = integration.PairSnapshotLifecycleTests.tearDown
    state = recovery.PairOrderRecoveryTests.state
    broker = recovery.PairOrderRecoveryTests.broker
    no_writes = recovery.PairOrderRecoveryTests.no_writes

    def seed(self):
        original = recovery.seed_pending(self.engine, BEFORE)
        for index, leg in enumerate(original["pending"]["legs"]):
            leg["receipt"] = recovery.receipt_for(leg, status="FILLED" if index else "REJECTED",
                                                 qty="0.2" if index else "0")
        self.f.store.put("pair_runtime:gold", original)
        return original

    def assert_retained(self, original):
        self.assertIsNotNone(self.state()["pending"])
        self.assertEqual(self.state()["owned"], original["owned"])
        self.assertEqual(self.state()["daily_volume"], original["daily_volume"])
        self.assertIsNone(self.f.store.get("pair_batch:" + original["pending"]["id"]))

    def test_manual_adoption_reloads_modes_and_all_account_open_orders(self):
        self.seed()
        with self.no_writes(), ExitStack() as stack:
            snapshots = [stack.enter_context(patch.object(broker, "snapshot", wraps=broker.snapshot))
                         for broker in self.live.values()]
            result = self.engine.pairs.check_recovery("gold")
        self.assertTrue(result["completed"], result)
        for snapshot in snapshots:
            self.assertTrue(any(call.kwargs.get("fresh_modes") is True for call in snapshot.call_args_list))
        for broker in self.live.values():
            reads = [call for call in broker.api.calls if call[1] == "/fapi/v3/openOrders"]
            self.assertTrue(reads)
            for method, _, args, kwargs in reads:
                self.assertEqual(method, "GET")
                self.assertTrue(kwargs["signed"])
                self.assertFalse(args, "A symbol filter would miss other-market orders")

    def test_other_market_open_order_blocks_adoption(self):
        original = self.seed()
        self.broker().api.responses["/fapi/v3/openOrders"] = [{"symbol": "SPCXUSD1", "orderId": "external"}]
        with self.no_writes():
            result = self.engine.pairs.check_recovery("gold")
        self.assertFalse(result["completed"])
        self.assert_retained(original)

    def test_account_event_after_final_reads_revokes_commit_guard(self):
        original = self.seed()
        read = self.engine.pairs._read_members

        def invalidate_after_read(*args, **kwargs):
            value = read(*args, **kwargs)
            self.broker()._cycle_account_event("ACCOUNT_UPDATE")
            return value

        with self.no_writes(), patch.object(self.engine.pairs, "_read_members", side_effect=invalidate_after_read):
            result = self.engine.pairs.check_recovery("gold")
        self.assertFalse(result["completed"])
        self.assert_retained(original)
