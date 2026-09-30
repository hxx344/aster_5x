"""Legacy cycle opens are explicitly archived as unknown, never inferred rejected."""
from contextlib import ExitStack
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_order_recovery as recovery
from tests.test_pair_order_recovery import BATCH_ID, receipt_for, seed_pending
from tests.test_pair_notional_rejection import ORIGINAL_REJECTION
from trading.exchange import AmbiguousOrder, ExchangeError
from trading.models import TradingError
from trading.pair_execution import PairTrader


class PairCycleOrderRecoveryTests(TestCase):
    setUp = recovery.PairOrderRecoveryTests.setUp
    state = recovery.PairOrderRecoveryTests.state
    broker = recovery.PairOrderRecoveryTests.broker
    no_writes = recovery.PairOrderRecoveryTests.no_writes
    preview = recovery.PairOrderRecoveryTests.preview
    confirm = recovery.PairOrderRecoveryTests.confirm
    audit = recovery.PairOrderRecoveryTests.audit
    event_rows = recovery.PairOrderRecoveryTests.event_rows
    change_state = recovery.PairOrderRecoveryTests.change_state

    # The same archival transaction must retain its protections for cycles.
    test_archive_retains_original_unknown_batch_and_accounting_without_writes = (
        recovery.PairOrderRecoveryTests.test_archive_retains_original_unknown_batch_and_accounting_without_writes)
    test_equal_actual_positions_that_differ_from_before_are_rejected = (
        recovery.PairOrderRecoveryTests.test_equal_actual_positions_that_differ_from_before_are_rejected)
    test_running_group_and_each_pending_transfer_state_are_rejected = (
        recovery.PairOrderRecoveryTests.test_running_group_and_each_pending_transfer_state_are_rejected)
    test_full_or_malformed_order_history_fails_closed = (
        recovery.PairOrderRecoveryTests.test_full_or_malformed_order_history_fails_closed)
    test_receipt_appearing_between_preview_and_confirm_prevents_archive = (
        recovery.PairOrderRecoveryTests.test_receipt_appearing_between_preview_and_confirm_prevents_archive)
    test_changed_batch_or_actual_positions_invalidates_confirmation = (
        recovery.PairOrderRecoveryTests.test_changed_batch_or_actual_positions_invalidates_confirmation)
    test_acknowledgment_is_required_and_token_expiry_and_replay_are_safe = (
        recovery.PairOrderRecoveryTests.test_acknowledgment_is_required_and_token_expiry_and_replay_are_safe)
    test_unsafe_pending_shapes_and_tracking_block_preview = (
        recovery.PairOrderRecoveryTests.test_unsafe_pending_shapes_and_tracking_block_preview)
    test_other_store_mutation_during_confirmation_is_not_overwritten = (
        recovery.PairOrderRecoveryTests.test_other_store_mutation_during_confirmation_is_not_overwritten)
    test_failure_writing_audit_or_event_rolls_back_archival = (
        recovery.PairOrderRecoveryTests.test_failure_writing_audit_or_event_rolls_back_archival)
    test_late_archived_receipt_blocks_paper_transfer_before_any_wallet_change = (
        recovery.PairOrderRecoveryTests.test_late_archived_receipt_blocks_paper_transfer_before_any_wallet_change)

    def seed(self, before=None):
        state = seed_pending(self.engine, before)
        pair = self.f.store.pair("gold")
        self.f.store.save_pair({**pair, "ordinary": {**pair["ordinary"], "enabled": False},
                               "cycle": {**pair["cycle"], "enabled": True}})
        state["pending"]["kind"] = "cycle"
        for leg in state["pending"]["legs"]:
            leg.update(submit_evidence_version=1, submit_error=ORIGINAL_REJECTION.replace("-2029", "-5018"))
        self.f.store.put("pair_runtime:gold", state)
        return deepcopy(state)

    def missing_orders(self):
        stack = ExitStack()
        for key in ("long", "short"):
            stack.enter_context(patch.object(self.broker(key), "query", side_effect=
                ExchangeError("Order does not exist.", code=-2013, http_status=400)))
        return stack

    def test_v1_rejection_text_stays_unknown_until_explicit_archive(self):
        original = self.seed()
        with self.no_writes(), self.missing_orders():
            self.engine.pairs.tick("gold")
            self.assertTrue(all(leg["receipt"] is None for leg in self.state()["pending"]["legs"]))
            checked = self.engine.pairs.check_recovery("gold")
            self.assertFalse(checked["completed"])
            self.assertTrue(checked["archive_available"])
            self.assertEqual([row["status"] for row in checked["orders"]], ["UNKNOWN", "UNKNOWN"])
            self.confirm(self.preview()["token"])
        saved = self.state()
        self.assertIsNone(saved["pending"])
        self.assertEqual(saved["phase"], "paused")
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertEqual(saved["owned"], original["owned"])
        self.assertEqual(saved["daily_volume"], original["daily_volume"])
        self.assertEqual(saved["progress"]["completed_cycles"], original["progress"]["completed_cycles"])
        self.assertIn("循环开仓", saved["reason"])
        self.assertTrue(all(leg["receipt"] is None for leg in self.audit()["pending"]["legs"]))
        self.assertIsNone(self.f.store.get("pair_batch:" + BATCH_ID))

    def test_cycle_shape_must_be_unstarted_with_two_unknown_originals(self):
        original = self.seed()
        mutations = {
            "baseline_missing": lambda s: s["progress"].pop("baseline"),
            "baseline_changed": lambda s: s["progress"]["baseline"].update(SHORT="3.040"),
            "baseline_invalid": lambda s: s["progress"]["baseline"].update(SHORT="NaN"),
            "phase_missing": lambda s: s["progress"].pop("phase"),
            "closing_cycle": lambda s: s["progress"].update(phase="waiting_close"),
            "tiny_increment": lambda s: s["progress"]["quantities"].update(LONG="0.00000000000000000001"),
            "increment_missing": lambda s: s["progress"]["quantities"].pop("SHORT"),
            "prepared": lambda s: s["pending"]["legs"][0].update(dispatch="prepared"),
            "receipt_missing": lambda s: s["pending"]["legs"][0].pop("receipt"),
            "active_receipt": lambda s: s["pending"]["legs"][0].update(receipt=receipt_for(s["pending"]["legs"][0], status="NEW")),
            "terminal_receipt": lambda s: s["pending"]["legs"][0].update(receipt=receipt_for(s["pending"]["legs"][0])),
            "repair_attempt": lambda s: s["pending"].update(repair_attempts=1),
        }
        for label, mutate in mutations.items():
            with self.subTest(boundary=label):
                changed = deepcopy(original)
                mutate(changed)
                self.f.store.put("pair_runtime:gold", changed)
                with self.no_writes(), self.assertRaises(TradingError):
                    self.engine.pairs.check_recovery("gold")
                with self.no_writes(), self.assertRaises(TradingError):
                    self.preview()
                self.assertEqual(self.state(), changed)
                self.assertIsNone(self.audit())

    def test_cycle_cannot_skip_positions_or_take_ordinary_start_recovery(self):
        original = self.seed()
        with self.no_writes():
            with self.assertRaises(TradingError):
                self.engine.pairs.skip_recovery("gold", BATCH_ID, True)
            with self.assertRaises(TradingError):
                self.engine.pairs.recovery.check("gold", source="paused_start")
            with self.assertRaises(TradingError):
                self.engine.pairs.enable("gold", True)
        self.assertEqual(self.state(), original)
        self.assertFalse(self.f.store.pair("gold")["enabled"])

    def test_real_receipt_is_restored_instead_of_archiving_unknown(self):
        original = self.seed()
        for status, qty in (("NEW", "0"), ("PARTIALLY_FILLED", "0.1"), ("FILLED", "0.2")):
            with self.subTest(status=status):
                self.f.store.put("pair_runtime:gold", original)
                row = receipt_for(original["pending"]["legs"][0], status=status, qty=qty)
                with self.no_writes(), patch.object(self.broker(), "query", return_value=row):
                    result = self.preview()
                self.assertEqual(result["status"], "receipts_found")
                self.assertEqual(self.state()["pending"]["legs"][0]["receipt"], row)
                self.assertIsNone(self.audit())
                self.assertIsNone(self.state().get("recovery_watch"))

    def test_ambiguous_or_failed_queries_never_authorize_archive(self):
        original = self.seed()
        for error in (ExchangeError("gateway", code=-2013, http_status=503),
                      AmbiguousOrder("unknown", code=-2013),
                      ExchangeError("cooldown", code=-2013, retry_after=20)):
            with self.subTest(error=type(error).__name__), self.no_writes(), \
                    patch.object(self.broker(), "query", side_effect=error), self.assertRaises(TradingError):
                self.preview()
            self.assertEqual(self.state(), original)

    def test_restarted_archive_watch_blocks_late_fills_before_new_orders_and_transfers(self):
        from trading.pair_recovery import require_archived_orders_clear
        original = self.seed()
        with self.no_writes(), self.missing_orders():
            self.confirm(self.preview()["token"])
        pair = self.f.store.save_pair({**self.f.store.pair("gold"), "enabled": True})
        # Rebuild the trader: protection comes from the durable watcher.
        trader = PairTrader(self.engine)
        for status, qty in (("NEW", "0"), ("FILLED", "0.2")):
            row = receipt_for(original["pending"]["legs"][0], status=status, qty=qty)
            with self.subTest(status=status), self.no_writes(), patch.object(self.broker(), "query", return_value=row):
                with self.assertRaises(TradingError):
                    trader._config_guard(pair, original["identities"], opening=True)
                # MarginBalancer uses this same guard before preparing a new transfer.
                with self.assertRaises(TradingError):
                    require_archived_orders_clear(self.engine, pair)
        self.assertIsNotNone(self.state()["recovery_watch"])
