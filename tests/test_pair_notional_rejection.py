"""Offline -2029 rejection and upgrade recovery, without exchange connections."""
from contextlib import ExitStack
from copy import deepcopy
import time
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_order_recovery as fixtures
from trading.exchange import AmbiguousOrder, BudgetWait, ExchangeError, RequestNotSent
from trading.execution import Executor
from trading.models import dec, wire
from trading.pair_execution import PairTrader


SYMBOL = "XAUUSD1"
ORIGINAL_REJECTION = ("Aster 拒绝请求（代码 -2029）：You've reached the maximum notional value limit for this symbol. "
                      "You can still reduce or close your position to manage your risk.")
REPORTED_REJECTION = ("Aster 拒绝请求（代码 -2029）：You’ve reached the maximum notional value limit for this symbol. "
                      "You can still reduce or close your position to manage your risk.")


class PairNotionalRejectionTests(TestCase):
    setUp = fixtures.PairExplicitOrderRejectionTests.setUp
    tick = fixtures.PairExplicitOrderRejectionTests.tick
    snapshots = fixtures.PairExplicitOrderRejectionTests.snapshots

    def assert_baseline(self):
        for key, side, index in (("long", "LONG", 0), ("short", "SHORT", 1)):
            self.assertEqual(self.snapshots()[key].pair(SYMBOL)[index].qty, dec(self.before[side]))

    def seed_legacy(self):
        """The screenshot's pre-upgrade intent: A unknown, B filled 1.209."""
        state = self.store.get("pair_runtime:gold")
        identities = self.trader._members(self.store.pair("gold"))[2]
        qty = dec("1.209")
        orders = {key: Executor.order(SYMBOL, side, direction, qty, "legacy_" + key)
                  for key, side, direction in (("long", "LONG", "BUY"), ("short", "SHORT", "SELL"))}
        filled = self.brokers["short"].submit([orders["short"]])[0]
        state.update(identities=identities, phase="reconciling", pending={
            "id": "legacy2029", "kind": "ordinary", "phase": "open", "symbol": SYMBOL,
            "identities": deepcopy(identities), "created_at": time.time() - 180,
            "quantity": wire(qty), "before": deepcopy(self.before),
            "target": {side: wire(dec(value) + qty) for side, value in self.before.items()},
            "leverage": 20, "legs": [
                {"key": "long", "order": orders["long"], "receipt": None, "dispatch": "sending",
                 "submit_error": ORIGINAL_REJECTION, "error": "Aster 拒绝请求（代码 -2013）：Order does not exist."},
                {"key": "short", "order": orders["short"], "receipt": filled, "dispatch": "sending"}],
            "repairs": [], "repair_attempts": 0, "config": {}})
        self.store.put("pair_runtime:gold", state)
        return state

    def test_new_rejection_reduces_only_successful_side_and_preserves_baseline(self):
        for rejected in ("long", "short"):
            with self.subTest(rejected=rejected):
                successful = "short" if rejected == "long" else "long"
                broker = self.brokers[successful]
                with patch.object(self.brokers[rejected], "submit", side_effect=ExchangeError(
                        ORIGINAL_REJECTION, code=-2029)) as rejected_send, \
                        patch.object(self.brokers[rejected], "query", side_effect=AssertionError("known rejection")), \
                        patch.object(broker, "submit", wraps=broker.submit) as successful_send:
                    state = self.tick()
                    self.assertEqual(state["phase"], "repairing", state)
                    state = self.tick()
                self.assertIsNone(state["pending"], state)
                self.assertFalse(state["last_batch"]["completed"])
                self.assertEqual(rejected_send.call_count, 1)
                self.assertEqual(successful_send.call_count, 2)
                orders = [call.args[0][0] for call in successful_send.call_args_list]
                self.assertEqual([order["side"] for order in orders],
                                 ["SELL", "BUY"] if successful == "short" else ["BUY", "SELL"])
                self.assertEqual(orders[0]["quantity"], orders[1]["quantity"])
                self.assert_baseline()
                # Allow the second independent attempt without a real-time wait.
                state["retry_at"] = 0
                self.store.put("pair_runtime:gold", state)

    def test_two_rejections_finish_zero_fill_batch(self):
        with ExitStack() as stack:
            for broker in self.brokers.values():
                stack.enter_context(patch.object(broker, "submit", side_effect=ExchangeError(ORIGINAL_REJECTION, code=-2029)))
                stack.enter_context(patch.object(broker, "query", side_effect=AssertionError("known rejection")))
            state = self.tick()
        self.assertIsNone(state["pending"], state)
        batch = self.store.get("pair_batch:" + state["last_batch"]["id"])
        self.assertTrue(all(leg["receipt"]["reject_code"] == -2029 for leg in batch["legs"]))
        self.assertTrue(all(leg["receipt"]["executedQty"] == "0" for leg in batch["legs"]))
        self.assertTrue(all(not broker.state["orders"] for broker in self.brokers.values()))
        self.assert_baseline()

    def test_structured_order_error_response_is_also_definitive(self):
        with patch.object(self.brokers["long"], "submit", return_value=[{"code": -2029, "msg": "notional cap"}]):
            state = self.tick()
        self.assertEqual(state["phase"], "repairing", state)
        receipt = state["pending"]["legs"][0]["receipt"]
        self.assertEqual((receipt["status"], receipt["executedQty"], receipt["reject_code"]), ("REJECTED", "0", -2029))
        self.tick()
        self.assert_baseline()

    def test_current_ambiguous_submission_can_never_use_legacy_text_recovery(self):
        with patch.object(self.brokers["long"], "submit", side_effect=AmbiguousOrder(ORIGINAL_REJECTION, code=-2029)), \
                patch.object(self.brokers["long"], "query", side_effect=ExchangeError("not found", code=-2013)):
            state = self.tick()
            self.assertEqual(state["pending"]["legs"][0]["submit_evidence_version"], 1)
            self.trader = PairTrader(self.engine)
            with patch.object(self.brokers["short"], "submit", side_effect=AssertionError("do not reduce unknown")):
                state = self.tick()
        self.assertEqual(state["phase"], "reconciling", state)
        self.assertIsNone(state["pending"]["legs"][0]["receipt"])
        self.assertEqual(state["pending"]["repairs"], [])

    def test_legacy_screenshot_recovers_after_restart_while_paused(self):
        original = self.seed_legacy()
        self.store.save_pair({**self.store.pair("gold"), "enabled": False})
        self.trader = PairTrader(self.engine)
        short = self.brokers["short"]
        with patch.object(self.brokers["long"], "query", side_effect=ExchangeError("not found", code=-2013, http_status=400)) as query, \
                patch.object(self.brokers["long"], "submit", side_effect=AssertionError("no missing leg addition")), \
                patch.object(short, "submit", wraps=short.submit) as reduce:
            state = self.tick()
            self.assertEqual(state["phase"], "repairing", state)
            state = self.tick()
            self.tick()
        self.assertIsNone(state["pending"])
        self.assertFalse(state["last_batch"]["completed"])
        self.assertEqual(query.call_count, 1)
        query.assert_called_with(SYMBOL, original["pending"]["legs"][0]["order"]["newClientOrderId"])
        self.assertEqual(reduce.call_count, 1)
        order = reduce.call_args.args[0][0]
        self.assertEqual((order["side"], order["positionSide"], order["quantity"]), ("BUY", "SHORT", "1.209"))
        self.assert_baseline()
        batch = self.store.get("pair_batch:legacy2029")
        self.assertEqual(batch["legs"][0]["submit_error"], ORIGINAL_REJECTION)
        self.assertTrue(batch["legs"][0]["receipt"]["recovered_from_submit_error"])
        self.assertEqual(batch["legs"][0]["receipt"]["reject_code"], -2029)

    def test_real_matching_fill_takes_precedence_over_old_error_text(self):
        original = self.seed_legacy()
        self.brokers["long"].submit([original["pending"]["legs"][0]["order"]])
        with patch.object(self.brokers["short"], "submit", side_effect=AssertionError("both filled")):
            state = self.tick()
        self.assertIsNone(state["pending"], state)
        self.assertTrue(state["last_batch"]["completed"])
        self.assertEqual(state["owned"], original["pending"]["target"])

    def test_reported_curly_apostrophe_rejection_manual_check_then_background_recovery(self):
        state = self.seed_legacy()
        state["pending"]["legs"][0]["submit_error"] = REPORTED_REJECTION
        self.store.put("pair_runtime:gold", state)
        self.store.save_pair({**self.store.pair("gold"), "enabled": False})
        short = self.brokers["short"]
        with patch.object(self.brokers["long"], "query", side_effect=ExchangeError("not found", code=-2013, http_status=400)) as query, \
                patch.object(self.brokers["long"], "submit", side_effect=AssertionError("no missing leg addition")), \
                patch.object(short, "submit", side_effect=AssertionError("manual check must not trade")):
            result = self.engine.pairs.check_recovery("gold")
        self.assertFalse(result["completed"])
        self.assertFalse(result["archive_available"])
        self.assertEqual([(row["status"], row["executed_qty"]) for row in result["orders"]],
                         [("REJECTED", "0"), ("FILLED", "1.209")])
        self.assertEqual(result["orders"][0]["error"], REPORTED_REJECTION)
        self.assertIn("仍需减仓", result["message"])
        query.assert_called_once_with(SYMBOL, "legacy_long")
        state = self.store.get("pair_runtime:gold")
        self.assertEqual(state["pending"]["repairs"], [])
        self.trader = PairTrader(self.engine)
        with patch.object(self.brokers["long"], "query", side_effect=AssertionError("saved rejection")), \
                patch.object(self.brokers["long"], "submit", side_effect=AssertionError("no missing leg addition")), \
                patch.object(short, "submit", wraps=short.submit) as reduce:
            self.assertEqual(self.tick()["phase"], "repairing")
            self.assertIsNone(self.tick()["pending"])
            self.tick()
        self.assertEqual(reduce.call_count, 1)
        order = reduce.call_args.args[0][0]
        self.assertEqual((order["side"], order["positionSide"], order["quantity"]), ("BUY", "SHORT", "1.209"))
        self.assert_baseline()
        self.assertFalse(self.store.pair("gold")["enabled"])
        leg = self.store.get("pair_batch:legacy2029")["legs"][0]
        self.assertEqual(leg["submit_error"], REPORTED_REJECTION)
        self.assertEqual(leg["receipt"]["reject_reason"], REPORTED_REJECTION)
        self.assertTrue(leg["receipt"]["recovered_from_submit_error"])

    def test_active_order_receipt_prevents_legacy_reclassification(self):
        original = self.seed_legacy()
        order = original["pending"]["legs"][0]["order"]
        active = {**order, "clientOrderId": order["newClientOrderId"], "status": "NEW", "executedQty": "0", "avgPrice": "0"}
        with patch.object(self.brokers["long"], "query", return_value=active), \
                patch.object(self.brokers["short"], "submit", side_effect=AssertionError("still active")):
            state = self.tick()
        self.assertEqual(state["phase"], "reconciling", state)
        self.assertEqual(state["pending"]["legs"][0]["receipt"]["status"], "NEW")

    def test_no_original_rejection_or_other_text_cannot_prove_zero_fill(self):
        original = self.seed_legacy()
        messages = (None, "timeout", ORIGINAL_REJECTION.replace("-2029", "-1007"),
                    "untrusted prefix " + ORIGINAL_REJECTION, ORIGINAL_REJECTION + "；接口冷却中",
                    REPORTED_REJECTION.replace("-2029", "-1007"),
                    "untrusted prefix " + REPORTED_REJECTION, REPORTED_REJECTION + "；接口冷却中",
                    "Aster 拒绝请求（代码 -2029）：unknown message")
        for message in messages:
            with self.subTest(message=message):
                state = deepcopy(original)
                state["pending"]["legs"][0]["submit_error"] = message
                self.store.put("pair_runtime:gold", state)
                with patch.object(self.brokers["long"], "query", side_effect=ExchangeError("not found", code=-2013)), \
                        patch.object(self.brokers["short"], "submit", side_effect=AssertionError("unproven rejection")):
                    state = self.tick()
                self.assertIsNone(state["pending"]["legs"][0]["receipt"])
                self.assertEqual(state["phase"], "reconciling")

    def test_failed_or_throttled_lookup_is_not_absence_evidence(self):
        original = self.seed_legacy()
        errors = (BudgetWait("budget", retry_after=20), ExchangeError("timeout"),
                  AmbiguousOrder("unknown", code=-2013), RequestNotSent("not sent", code=-2013),
                  ExchangeError("not found", code=-2013, retry_after=20),
                  ExchangeError("gateway", code=-2013, http_status=503),
                  ExchangeError("limited", code=-2013, http_status=429))
        for error in errors:
            with self.subTest(error=type(error).__name__, status=error.http_status):
                self.store.put("pair_runtime:gold", original)
                with patch.object(self.brokers["long"], "query", side_effect=error), \
                        patch.object(self.brokers["short"], "submit", side_effect=AssertionError("lookup failed")):
                    state = self.tick()
                self.assertIsNone(state["pending"]["legs"][0]["receipt"])
                self.assertEqual(state["phase"], "reconciling")

    def test_changed_actual_position_blocks_reduction(self):
        self.seed_legacy()
        broker = self.brokers["short"]
        broker.state["positions"][SYMBOL + ":SHORT"]["qty"] = "1.510"
        broker.save()
        with patch.object(self.brokers["long"], "query", side_effect=ExchangeError("not found", code=-2013)), \
                patch.object(broker, "submit", side_effect=AssertionError("actual does not match receipt")):
            state = self.tick()
        self.assertEqual(state["phase"], "attention", state)
        self.assertIsNotNone(state["pending"])
        self.assertEqual(state["pending"]["repairs"], [])

    def test_budget_wait_after_legacy_classification_preserves_recovery_across_restart(self):
        self.seed_legacy()
        with patch.object(self.brokers["long"], "query", side_effect=ExchangeError("not found", code=-2013)), \
                patch.object(self.trader, "_read", side_effect=BudgetWait("budget", retry_after=20)):
            state = self.tick()
        self.assertEqual(state["retry_after"], 20)
        self.assertEqual(state["pending"]["legs"][0]["receipt"]["status"], "REJECTED")
        self.assertEqual(state["pending"]["repairs"], [])
        self.trader = PairTrader(self.engine)
        with patch.object(self.brokers["long"], "query", side_effect=AssertionError("saved rejection")):
            self.assertEqual(self.tick()["phase"], "repairing")
            self.assertIsNone(self.tick()["pending"])
        self.assert_baseline()

    def test_legacy_recovery_does_not_apply_to_closing_or_repair_orders(self):
        original = self.seed_legacy()
        for repair in (False, True):
            with self.subTest(repair=repair):
                state = deepcopy(original)
                if repair:
                    state["pending"]["repairs"] = [state["pending"]["legs"].pop(0)]
                else:
                    state["pending"]["phase"] = "close"
                self.store.put("pair_runtime:gold", state)
                with patch.object(self.brokers["long"], "query", side_effect=ExchangeError("not found", code=-2013)), \
                        patch.object(self.brokers["short"], "submit", side_effect=AssertionError("not original opening")):
                    result = self.tick()
                self.assertEqual(result["phase"], "reconciling")
                leg = result["pending"]["repairs"][0] if repair else result["pending"]["legs"][0]
                self.assertIsNone(leg["receipt"])

    def test_changed_account_identity_blocks_lookup_and_reduction(self):
        state = self.seed_legacy()
        state["pending"]["identities"]["long"]["env_prefix"] = "ASTER_REPLACED"
        self.store.put("pair_runtime:gold", state)
        with patch.object(self.brokers["long"], "query", side_effect=AssertionError("wrong identity")), \
                patch.object(self.brokers["short"], "submit", side_effect=AssertionError("wrong identity")):
            state = self.tick()
        self.assertEqual(state["phase"], "attention", state)
        self.assertIsNotNone(state["pending"])
