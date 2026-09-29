"""Preserve manually adjusted ordinary positions using offline paper ledgers."""
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import sqlite3
import unittest
from unittest.mock import patch

from tests import test_pair_integration as integration
from tests import test_pair_manual_baseline as manual
from tests import test_pair_order_recovery as recovery
from trading.engine import Engine
from trading.exchange import BudgetWait, ExchangeError
from trading.execution import Executor
from trading.models import TradingError, dec, wire
from trading.pair_planning import PairRecoveryConflict
from trading.store import Store


SYMBOL = recovery.SYMBOL


class PairStartRecoveryTests(unittest.TestCase):
    setUp = integration.PairIntegrationTests.setUp
    seed = manual.PairManualBaselineTests.seed
    state = recovery.PairOrderRecoveryTests.state
    broker = recovery.PairOrderRecoveryTests.broker
    change_state = recovery.PairOrderRecoveryTests.change_state

    @contextmanager
    def no_writes(self):
        with ExitStack() as stack:
            writes = []
            for key in ("long", "short"):
                for method in ("submit", "cancel", "set_leverage", "set_cycle_leverage"):
                    writes.append(stack.enter_context(patch.object(self.broker(key), method,
                                                    side_effect=AssertionError("must preserve actual positions"))))
            for method in ("tick", "_paper", "_submit_live"):
                writes.append(stack.enter_context(patch("trading.margin_balance.MarginBalancer." + method,
                                          side_effect=AssertionError("must not transfer"))))
            try:
                yield
            finally:
                for write in writes:
                    write.assert_not_called()

    def balance(self, quantity="10"):
        external = []
        for key, side, index in (("long", "LONG", 0), ("short", "SHORT", 1)):
            broker = self.broker(key)
            current = broker.snapshot([SYMBOL]).pair(SYMBOL)[index].qty
            delta = dec(quantity) - current
            if not delta:
                continue
            buy = (side == "LONG") == (delta > 0)
            order = Executor.order(SYMBOL, side, "BUY" if buy else "SELL", abs(delta), "external-" + key)
            receipt = broker.submit([order])[0]
            self.assertEqual(receipt["status"], "FILLED")
            external.append(receipt)
        return external

    def assert_positions(self, quantity):
        for key, index in (("long", 0), ("short", 1)):
            self.assertEqual(self.broker(key).snapshot([SYMBOL]).pair(SYMBOL)[index].qty, dec(quantity))

    def assert_retained(self, original):
        state = self.state()
        self.assertEqual(state["pending"]["id"], original["pending"]["id"])
        self.assertEqual(state["owned"], original["owned"])
        self.assertEqual(state["daily_volume"], original["daily_volume"])
        self.assertIsNone(self.f.store.get("pair_batch:" + original["pending"]["id"]))
        self.assertFalse(self.f.store.pair("gold")["enabled"])

    def assert_adopted(self, original, quantity, source):
        state = self.state()
        owned = {"LONG": quantity, "SHORT": quantity}
        self.assertIsNone(state["pending"])
        self.assertEqual({side: dec(qty) for side, qty in state["owned"].items()},
                         {side: dec(quantity) for side in owned})
        self.assertEqual(state["progress"]["baseline"], state["owned"])
        self.assertEqual(state["progress"]["quantities"], {"LONG": "0", "SHORT": "0"})
        self.assertEqual(state["progress"]["completed_cycles"], original["progress"]["completed_cycles"])
        self.assertNotIn("attention", state)
        batch = self.f.store.get("pair_batch:" + original["pending"]["id"])
        self.assertEqual(batch["legs"], original["pending"]["legs"])
        self.assertEqual(batch["repairs"], original["pending"]["repairs"])
        audit = batch["manual_position_reconciliation"]
        self.assertEqual(audit["source"], source)
        self.assertEqual(audit["before"], original["owned"])
        self.assertEqual(audit["actual"], owned)
        self.assertEqual(audit["adopted_baseline"], owned)
        for side in owned:
            self.assertEqual(dec(audit["external_delta"][side]),
                             dec(quantity) - dec(audit["known_expected"][side]))
        expected_volume = deepcopy(original["daily_volume"])
        for leg in original["pending"]["legs"] + original["pending"]["repairs"]:
            receipt = leg["receipt"]
            if not dec(receipt["executedQty"]):
                continue
            date = datetime.fromtimestamp(receipt["updateTime"] / 1000, timezone.utc).date().isoformat()
            daily = expected_volume.setdefault(date, {"long": "0", "short": "0"})
            daily[leg["key"]] = wire(dec(daily[leg["key"]]) + dec(receipt["executedQty"]) * dec(receipt["avgPrice"]))
        self.assertEqual(state["daily_volume"], expected_volume)
        self.assert_positions(quantity)
        return deepcopy(state), batch

    def test_start_preserves_large_manual_additions_and_books_only_original_fills(self):
        original = self.seed()
        external = self.balance()
        with self.no_writes():
            self.assertTrue(self.engine.pairs.enable("gold", True)["enabled"])
        state, batch = self.assert_adopted(original, "10", "paused_start")
        for receipt in external:
            self.assertNotIn(receipt["clientOrderId"], str(batch))
        self.engine.pairs.enable("gold", False)
        restarted = Engine(Store(self.f.store.path), market=self.f.market)
        self.addCleanup(restarted.dashboard_reports.close)
        with patch("trading.paper.PaperBroker.submit", side_effect=AssertionError("no repeat repair")):
            restarted.pairs.tick("gold")
        self.assertEqual(self.state()["daily_volume"], state["daily_volume"])
        self.assertEqual(self.state()["owned"], state["owned"])
        self.assertEqual(self.f.store.get("pair_batch:" + batch["id"]), batch)

    def test_manual_check_accepts_new_balanced_baseline_without_enabling(self):
        original = self.seed()
        self.balance()
        with self.no_writes():
            self.assertTrue(self.engine.pairs.check_recovery("gold")["completed"])
        self.assert_adopted(original, "10", "paused_manual_check")
        self.assertFalse(self.f.store.pair("gold")["enabled"])

    def test_background_accepts_balanced_manual_additions_without_repair_or_attention(self):
        original = self.seed()
        self.balance()
        self.f.store.save_pair({**self.f.store.pair("gold"), "enabled": True})
        with self.no_writes():
            self.engine.pairs.tick("gold")
        state, _ = self.assert_adopted(original, "10", "automatic_positions")
        self.assertEqual(state["phase"], "waiting")
        self.assertTrue(self.f.store.pair("gold")["enabled"])

    def test_terminal_partial_fills_forming_equal_expected_positions_are_preserved(self):
        original = recovery.seed_pending(self.engine, {"LONG": "0.3", "SHORT": "0.4"})
        book = self.f.market.book(SYMBOL)
        with patch.object(self.f.market, "book", return_value=replace(book, ask_qty=dec("0.15"), bid_qty=dec("0.05"))):
            for leg in original["pending"]["legs"]:
                leg["receipt"] = self.broker(leg["key"]).submit([leg["order"]])[0]
                leg["error"] = None
        self.f.store.put("pair_runtime:gold", original)
        self.assertEqual([leg["receipt"]["status"] for leg in original["pending"]["legs"]], ["EXPIRED", "EXPIRED"])
        self.assert_positions("0.45")
        with self.no_writes():
            self.engine.pairs.enable("gold", True)
        _, batch = self.assert_adopted(original, "0.45", "paused_start")
        self.assertEqual(batch["manual_position_reconciliation"]["external_delta"], {"LONG": "0", "SHORT": "0"})

    def test_balanced_full_fills_over_margin_threshold_do_not_trigger_rollback(self):
        original = recovery.seed_pending(self.engine, {"LONG": "20", "SHORT": "20"})
        for leg in original["pending"]["legs"]:
            leg["receipt"] = self.broker(leg["key"]).submit([leg["order"]])[0]
            leg["error"] = None
        self.f.store.put("pair_runtime:gold", original)
        with self.no_writes():
            self.engine.pairs.tick("gold")
        self.assertIsNone(self.state()["pending"])
        self.assertEqual(self.state()["owned"], {"LONG": "20.2", "SHORT": "20.2"})
        self.assertNotIn("attention", self.state())
        self.assert_positions("20.2")

    def test_terminal_repair_and_exhausted_attempts_do_not_reduce_manual_additions(self):
        self.seed()
        order = Executor.order(SYMBOL, "SHORT", "BUY", dec("0.209"), "original-repair")
        receipt = self.broker("short").submit([order])[0]
        original = self.change_state(lambda state: state["pending"].update(repair_attempts=3, repairs=[
            {"key": "short", "order": order, "receipt": receipt, "dispatch": "sending"}]))
        self.balance()
        with self.no_writes():
            self.assertTrue(self.engine.pairs.check_recovery("gold")["completed"])
        self.assert_adopted(original, "10", "paused_manual_check")

    def test_unknown_or_active_original_and_repair_cannot_be_cleared_by_equal_positions(self):
        for repair in (False, True):
            for outcome in ("unknown", "NEW", "PARTIALLY_FILLED", "budget"):
                with self.subTest(repair=repair, outcome=outcome):
                    self.seed()
                    self.balance()
                    state = self.state()
                    if repair:
                        leg = {"key": "short", "dispatch": "sending", "receipt": None,
                               "order": Executor.order(SYMBOL, "SHORT", "BUY", dec("0.2"), "pending-repair")}
                        state["pending"].update(repairs=[leg], repair_attempts=1)
                    else:
                        leg = state["pending"]["legs"][0]
                    response = (recovery.receipt_for(leg, status=outcome,
                                qty="0.1" if outcome == "PARTIALLY_FILLED" else "0")
                                if outcome in {"NEW", "PARTIALLY_FILLED"} else None)
                    leg["receipt"] = response
                    self.f.store.put("pair_runtime:gold", state)
                    error = (BudgetWait("API budget unavailable", retry_after=30) if outcome == "budget"
                             else ExchangeError("order absent", code=-2013))
                    options = {"return_value": response} if response else {"side_effect": error}
                    with self.no_writes(), patch.object(self.broker(leg["key"]), "query", **options):
                        self.assertFalse(self.engine.pairs.check_recovery("gold")["completed"])
                        with self.assertRaises(TradingError):
                            self.engine.pairs.enable("gold", True)
                        self.engine.pairs.tick("gold")
                    self.assert_retained(state)
                    self.assert_positions("10")

    def test_cycle_ownership_and_pending_transfers_block_start_and_manual_adoption(self):
        initial = self.seed()
        self.balance()
        cases = ("cycle_batch", "cycle_position", "pending_transfer", "unknown_transfer")
        for case in cases:
            with self.subTest(case=case):
                state = deepcopy(initial)
                margin = {}
                if case == "cycle_batch":
                    state["pending"]["kind"] = "cycle"
                elif case == "cycle_position":
                    state["progress"]["quantities"]["LONG"] = "0.1"
                elif case == "pending_transfer":
                    margin = {"pending": {"id": "unresolved-transfer"}}
                else:
                    margin = {"status": "unknown"}
                self.f.store.put("pair_runtime:gold", state)
                self.f.store.put("pair_margin:gold", margin)
                with self.no_writes():
                    for action in (lambda: self.engine.pairs.enable("gold", True),
                                   lambda: self.engine.pairs.check_recovery("gold")):
                        with self.assertRaises(TradingError):
                            action()
                self.assert_retained(initial)
                self.assert_positions("10")

    def test_activation_failure_keeps_committed_new_baseline_paused_without_future_repair(self):
        original = self.seed()
        self.balance()
        with self.no_writes(), patch("trading.margin_balance.MarginBalancer.verify_members",
                                     side_effect=TradingError("activation verification failed")):
            with self.assertRaisesRegex(TradingError, "activation verification failed"):
                self.engine.pairs.enable("gold", True)
        saved, batch = self.assert_adopted(original, "10", "paused_start")
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        with self.no_writes():
            self.engine.pairs.tick("gold")
            self.engine.pairs.enable("gold", True)
        self.assertEqual({side: dec(qty) for side, qty in self.state()["owned"].items()},
                         {side: dec(qty) for side, qty in saved["owned"].items()})
        self.assertEqual(self.state()["daily_volume"], saved["daily_volume"])
        self.assertEqual(self.f.store.get("pair_batch:" + batch["id"]), batch)

    def test_archived_unknown_order_watch_still_blocks_changed_baseline(self):
        self.seed()
        self.balance()
        state = self.state()
        legs = [{"key": leg["key"], "order": {**leg["order"],
                 "newClientOrderId": "archived-" + leg["key"]}} for leg in state["pending"]["legs"]]
        state["recovery_watch"] = {"batches": [{"id": "archived", "identities": state["identities"], "legs": legs}]}
        self.f.store.put("pair_runtime:gold", state)
        with self.no_writes():
            self.assertFalse(self.engine.pairs.check_recovery("gold")["completed"])
            with self.assertRaises(TradingError):
                self.engine.pairs.enable("gold", True)
        self.assert_retained(state)
        self.assert_positions("10")

    def test_history_failure_rolls_back_baseline_batch_and_volume(self):
        original = self.seed()
        self.balance()
        with self.f.store.connect() as db:
            db.execute("CREATE TRIGGER reject_start_batch BEFORE INSERT ON kv "
                       "WHEN NEW.key='pair_batch:" + original["pending"]["id"] + "' "
                       "BEGIN SELECT RAISE(ABORT, 'history write failed'); END")
        with self.no_writes(), self.assertRaisesRegex(sqlite3.DatabaseError, "history write failed"):
            self.engine.pairs.enable("gold", True)
        self.assert_retained(original)
        self.assert_positions("10")

    def test_concurrent_runtime_or_margin_change_survives_all_outer_save_paths(self):
        original = self.seed()
        self.balance()
        other = Store(self.f.store.path)
        read = self.engine.pairs._read_members
        for action in ("check", "start", "background"):
            for target in ("runtime", "margin"):
                with self.subTest(action=action, target=target):
                    self.f.store.put("pair_runtime:gold", original)
                    self.f.store.put("pair_margin:gold", {})
                    injected = ({**deepcopy(original), "concurrent_marker": "must survive"}
                                if target == "runtime" else {"checked_at": 123, "concurrent_marker": "must survive"})

                    def change_after_read(*args, **kwargs):
                        result = read(*args, **kwargs)
                        other.put("pair_" + target + ":gold", injected)
                        return result

                    with self.no_writes(), patch.object(self.engine.pairs, "_read_members", side_effect=change_after_read):
                        if action == "background":
                            self.engine.pairs.tick("gold")
                        else:
                            with self.assertRaises(PairRecoveryConflict):
                                if action == "check":
                                    self.engine.pairs.check_recovery("gold")
                                else:
                                    self.engine.pairs.enable("gold", True)
                    self.assertEqual(self.f.store.get("pair_" + target + ":gold"), injected)
                    self.assert_retained(original)
                    if target == "margin":
                        self.assertEqual(self.state(), original)
                    self.assert_positions("10")
