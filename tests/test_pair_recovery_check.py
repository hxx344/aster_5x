"""Manual reconciliation shares recovery, but never dispatches writes."""
from contextlib import ExitStack
import time
import unittest
from unittest.mock import patch

from tests import test_pair_integration as integration
from tests import test_pair_order_recovery as recovery_tests
from tests.test_pair_order_recovery import seed_pending, receipt_for
from trading.exchange import ExchangeError, RateBudget, RequestNotSent
from trading.models import TradingError


class PairRecoveryCheckTests(unittest.TestCase):
    setUp = integration.PairIntegrationTests.setUp
    seed = recovery_tests.PairOrderRecoveryTests.seed
    state = recovery_tests.PairOrderRecoveryTests.state
    broker = recovery_tests.PairOrderRecoveryTests.broker
    change_state = recovery_tests.PairOrderRecoveryTests.change_state
    no_writes = recovery_tests.PairOrderRecoveryTests.no_writes

    def check(self):
        with self.no_writes():
            return self.engine.pairs.check_recovery("gold")

    def receipts(self, statuses=("EXPIRED", "EXPIRED")):
        def change(state):
            for leg, status in zip(state["pending"]["legs"], statuses):
                leg["receipt"] = receipt_for(leg, status=status, qty="0.2" if status == "FILLED" else "0")
                if status == "FILLED":
                    broker = self.broker(leg["key"])
                    broker.state["positions"]["XAUUSD1:" + leg["key"].upper()]["qty"] = "3.239"
                    broker.save()
        return self.change_state(change)

    def test_known_terminal_receipts_finish_without_requery_or_archive(self):
        original = self.seed()
        self.receipts()
        with patch.object(self.broker(), "query", side_effect=AssertionError("terminal must not requery")), \
             patch.object(self.broker("short"), "query", side_effect=AssertionError("terminal must not requery")):
            result = self.check()
        self.assertTrue(result["completed"])
        self.assertFalse(result["archive_available"])
        self.assertNotIn("token", result)
        self.assertIsNone(self.state()["pending"])
        self.assertEqual(self.state()["owned"], original["owned"])
        self.assertFalse(self.state()["last_batch"]["completed"])
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertEqual([row["status"] for row in result["orders"]], ["EXPIRED"] * 2)
        self.assertTrue(all(not row["error"] for row in result["orders"]))
        saved = self.state()
        with self.assertRaisesRegex(TradingError, "当前没有待核对批次"):
            self.check()
        self.assertEqual(self.state(), saved)

    def test_mixed_receipts_query_only_unknown_and_preserve_found_rows_after_finish(self):
        self.seed()
        current = self.receipts()
        found = current["pending"]["legs"][1]["receipt"]
        self.change_state(lambda state: state["pending"]["legs"][1].update(receipt=None))
        with patch.object(self.broker(), "query", side_effect=AssertionError("known terminal")), \
             patch.object(self.broker("short"), "query", return_value=found) as query:
            result = self.check()
        query.assert_called_once_with("XAUUSD1", found["clientOrderId"])
        self.assertTrue(result["completed"])
        self.assertEqual(result["orders"][1]["status"], "EXPIRED")
        self.assertEqual(result["orders"][1]["executed_qty"], "0")

    def test_successful_fills_are_booked_with_current_positions(self):
        original = self.seed()
        self.receipts(("FILLED", "FILLED"))
        result = self.check()
        self.assertTrue(result["completed"])
        self.assertTrue(self.state()["last_batch"]["completed"])
        self.assertEqual(self.state()["owned"], original["pending"]["target"])
        self.assertIsNotNone(self.f.store.get("pair_batch:" + original["pending"]["id"]))

    def test_partial_success_reports_repair_without_creating_or_sending_it(self):
        self.seed()
        self.receipts(("FILLED", "EXPIRED"))
        for _ in range(2):
            result = self.check()
            self.assertFalse(result["completed"])
            self.assertFalse(result["archive_available"])
            self.assertIn("仍需减仓", result["message"])
            self.assertEqual(self.state()["pending"]["repairs"], [])
            self.assertEqual(self.state()["pending"]["repair_attempts"], 0)
        # Background recovery retains its existing authority to reduce.
        self.engine.pairs.tick("gold")
        self.assertEqual(len(self.state()["pending"]["repairs"]), 1)

    def test_existing_repair_receipt_can_complete_the_same_batch(self):
        self.seed()
        self.receipts(("FILLED", "EXPIRED"))
        self.engine.pairs.tick("gold")
        self.assertEqual(len(self.state()["pending"]["repairs"]), 1)
        result = self.check()
        self.assertTrue(result["completed"])
        self.assertEqual(len(result["orders"]), 3)
        self.assertFalse(self.state()["last_batch"]["completed"])

    def test_exhausted_repair_limit_does_not_promise_background_reduction(self):
        self.seed()
        self.receipts(("FILLED", "EXPIRED"))
        self.change_state(lambda state: state["pending"].update(repair_attempts=3))
        result = self.check()
        self.assertFalse(result["completed"])
        self.assertIn("停止继续自动减仓", result["message"])
        self.assertEqual(self.state()["phase"], "attention")

    def test_budget_rejected_repairs_do_not_impose_a_false_order_count_limit(self):
        self.seed()
        self.receipts()
        def add_repairs(state):
            pending = state["pending"]
            for index in range(9):
                order = dict(pending["legs"][0]["order"], side="SELL", newClientOrderId=f"budget-repair-{index}")
                leg = {"key": "long", "order": order, "dispatch": "sending"}
                leg["receipt"] = dict(receipt_for(leg, status="REJECTED"), local_not_sent=True)
                pending["repairs"].append(leg)
        self.change_state(add_repairs)
        result = self.check()
        self.assertTrue(result["completed"])
        self.assertEqual(len(result["orders"]), 11)

    def test_mismatching_positions_retain_batch_and_report_attention(self):
        self.seed()
        self.receipts(("FILLED", "FILLED"))
        broker = self.broker()
        broker.state["positions"]["XAUUSD1:LONG"]["qty"] = "3.039"
        broker.save()
        result = self.check()
        self.assertFalse(result["completed"])
        self.assertIn("实际仓位或杠杆不一致", result["message"])
        self.assertEqual(self.state()["phase"], "attention")
        self.assertIsNotNone(self.state()["pending"])

    def test_real_style_not_found_stays_unknown_and_only_offers_archive_check(self):
        self.seed()
        with ExitStack() as stack:
            for side in ("long", "short"):
                stack.enter_context(patch.object(self.broker(side), "query", side_effect=ExchangeError("Order does not exist", code=-2013)))
            result = self.check()
        self.assertFalse(result["completed"])
        self.assertTrue(result["archive_available"])
        self.assertNotIn("token", result)
        self.assertTrue(all(row["status"] == "UNKNOWN" for row in result["orders"]))
        self.assertIsNotNone(self.state()["pending"])

    def test_budget_errors_are_visible_per_order_and_never_finalize(self):
        self.seed()
        with ExitStack() as stack:
            for side in ("long", "short"):
                stack.enter_context(patch.object(self.broker(side), "query", side_effect=RequestNotSent("本地 API 请求权重预算不足", retry_after=12)))
            result = self.check()
        self.assertFalse(result["completed"])
        self.assertTrue(all("预算不足" in row["error"] for row in result["orders"]))
        self.assertIsNotNone(self.state()["pending"])

    def test_malformed_order_names_side_and_field_without_any_query(self):
        self.seed()
        self.change_state(lambda state: state["pending"]["legs"][0]["order"].update(side="SELL"))
        original = self.state()
        with patch.object(self.broker(), "query", side_effect=AssertionError("invalid order")):
            with self.assertRaisesRegex(TradingError, "A · 只多.*side 字段"):
                self.check()
        self.assertEqual(self.state(), original)

    def test_known_receipt_is_not_subject_to_unknown_archive_age_limit(self):
        for created_at in (time.time() - 5, time.time() - 8 * 86400):
            self.seed()
            self.receipts()
            self.change_state(lambda state: state["pending"].update(created_at=created_at))
            self.assertTrue(self.check()["completed"])

    def test_running_or_wrong_identity_cannot_reconcile(self):
        self.seed()
        self.receipts()
        self.change_state(lambda state: state["pending"]["identities"]["long"].update(account_id="other"))
        with self.assertRaisesRegex(TradingError, "身份不一致"):
            self.check()
        self.seed()
        pair = self.f.store.pair("gold")
        pair["enabled"] = True
        self.f.store.save_pair(pair)
        with self.assertRaisesRegex(TradingError, "先暂停"):
            self.check()

    def test_manual_check_invalidates_previous_archive_preview(self):
        self.seed()
        preview = self.engine.pairs.preview_recovery("gold")
        self.check()
        with self.assertRaises(TradingError):
            self.engine.pairs.confirm_recovery("gold", preview["token"], acknowledge_unknown=True)


class PairRecoveryCheckLiveTests(unittest.TestCase):
    setUp = integration.PairSnapshotLifecycleTests.setUp
    tearDown = integration.PairSnapshotLifecycleTests.tearDown
    broker = recovery_tests.PairOrderRecoveryTests.broker
    no_writes = recovery_tests.PairOrderRecoveryTests.no_writes

    def test_live_gets_use_recovery_budget_and_no_unknown_order_is_resent(self):
        pending = seed_pending(self.engine)["pending"]
        seen = []
        with self.no_writes(), ExitStack() as stack:
            for leg in pending["legs"]:
                broker = self.broker(leg["key"])
                broker.api.responses["/fapi/v3/order"] = receipt_for(leg)
                broker.api.budget = RateBudget()
                original = broker.api.call
                def call(method, path, *args, broker=broker, original=original, **kwargs):
                    self.assertEqual(method, "GET")
                    self.assertTrue(getattr(broker.api.budget.priority, "reconciliation", False), path)
                    seen.append(path)
                    return original(method, path, *args, **kwargs)
                stack.enter_context(patch.object(broker.api, "call", side_effect=call))
            result = self.engine.pairs.check_recovery("gold")
        self.assertTrue(result["completed"])
        self.assertEqual(seen.count("/fapi/v3/order"), 2)
        self.assertNotIn("/fapi/v3/allOrders", seen)
