import json
from copy import deepcopy
from decimal import Decimal
from fractions import Fraction
import threading
import time
import unittest
from unittest.mock import Mock, patch

from trading.exchange import LiveBroker, SnapshotSuperseded
from trading.execution import Executor
from trading.models import TradingError
from trading.user_stream import PrivateAccountStream
from trading.ws_evidence import PrivateEventEvidence


SYMBOL = "XAUUSD1"
ORDER = {"symbol": SYMBOL, "newClientOrderId": "ws-order", "side": "BUY",
         "positionSide": "LONG", "type": "MARKET", "quantity": "2"}


def order_event(*, stamp=None, status="FILLED", quantity="2", client_id="ws-order"):
    stamp = int(time.time() * 1000) if stamp is None else stamp
    return {"e": "ORDER_TRADE_UPDATE", "E": stamp, "T": stamp - 1,
            "o": {"s": SYMBOL, "c": client_id, "S": "BUY", "ps": "LONG", "o": "MARKET",
                  "ot": "MARKET", "q": "2", "z": quantity, "ap": "2500" if Decimal(quantity) else "0",
                  "X": status, "i": 123, "T": stamp}}


def balance_event(*, stamp=None, delta="-10", wallet="90", reason="ASSET_TRANSFER"):
    stamp = int(time.time() * 1000) if stamp is None else stamp
    return {"e": "ACCOUNT_UPDATE", "E": stamp, "T": stamp,
            "a": {"m": reason, "B": [{"a": "USD1", "wb": wallet, "cw": wallet, "bc": delta}], "P": []}}


class PrivateEventEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.broker = LiveBroker(None, Mock(), api=Mock(budget=None))
        with patch.object(PrivateAccountStream, "start"):
            self.broker.start_cycle_hot_data([SYMBOL])
        self.stream = self.broker.cycle_stream
        self.stream._set_connected(True)
        self.addCleanup(self.broker.close)
        self.stamp = int(time.time() * 1000)

    def emit(self, payload):
        self.stream._handle_message(json.dumps(payload))

    def test_terminal_order_receipt_is_complete_fresh_detached_and_network_free(self):
        self.emit(order_event(stamp=self.stamp))
        receipt = self.broker.order_event_receipt(ORDER)
        Executor.validate_receipt(ORDER, receipt)
        self.assertEqual(receipt["origQty"], "2")
        self.assertEqual(receipt["orderId"], 123)
        self.assertEqual(receipt["updateTime"], self.stamp)
        receipt["executedQty"] = "0"
        self.assertEqual(self.broker.order_event_receipt(ORDER)["executedQty"], "2")
        self.broker.api.call.assert_not_called()
        self.broker.query(SYMBOL, ORDER["newClientOrderId"])
        self.broker.api.call.assert_called_once_with("GET", "/fapi/v3/order",
            {"symbol": SYMBOL, "origClientOrderId": ORDER["newClientOrderId"]}, signed=True)

    def test_nonterminal_order_does_not_delay_rest_and_progress_can_become_terminal(self):
        self.emit(order_event(stamp=self.stamp - 2, status="NEW", quantity="0"))
        self.assertIsNone(self.broker.order_event_receipt(ORDER))
        self.emit(order_event(stamp=self.stamp - 1, status="PARTIALLY_FILLED", quantity="1"))
        self.assertIsNone(self.broker.order_event_receipt(ORDER))
        self.emit(order_event(stamp=self.stamp))
        self.assertEqual(self.broker.order_event_receipt(ORDER)["status"], "FILLED")

    def test_order_identifiers_accept_exchange_long_range_independent_of_time_range(self):
        event = order_event(stamp=self.stamp)
        event["o"]["i"] = 123456789012345678
        self.emit(event)
        self.assertEqual(self.broker.order_event_receipt(ORDER)["orderId"], 123456789012345678)

    def test_order_fields_and_original_quantity_must_match_intent(self):
        for field, value in (("symbol", "CLUSD1"), ("newClientOrderId", "other"), ("side", "SELL"),
                             ("positionSide", "SHORT"), ("type", "LIMIT"), ("quantity", "3")):
            with self.subTest(field=field):
                self.broker._private_evidence.reset(True)
                self.emit(order_event(stamp=self.stamp))
                altered = {**ORDER, field: value}
                self.assertIsNone(self.broker.order_event_receipt(altered))

    def test_invalid_order_fields_never_provide_receipt(self):
        variants = [("q", "3"), ("q", "NaN"), ("z", "3"), ("z", "-1"), ("ap", "0"),
                    ("ap", "Infinity"), ("i", True), ("ot", "LIMIT"), ("o", "LIMIT"),
                    ("S", "SELL"), ("ps", "SHORT"), ("X", "BOGUS"), ("X", "REJECTED"), ("T", True)]
        for field, value in variants:
            with self.subTest(field=field, value=value):
                self.broker._private_evidence.reset(True)
                event = order_event(stamp=self.stamp)
                event["o"][field] = value
                self.emit(event)
                self.assertIsNone(self.broker.order_event_receipt(ORDER))
        for field in ("E", "T"):
            for value in (True, self.stamp - 31000, self.stamp + 2000):
                self.broker._private_evidence.reset(True)
                event = order_event(stamp=self.stamp)
                event[field] = value
                self.emit(event)
                self.assertIsNone(self.broker.order_event_receipt(ORDER))

    def test_exact_order_duplicate_is_idempotent_and_does_not_renew_freshness(self):
        self.emit(order_event(stamp=self.stamp))
        cached = self.broker._private_evidence.orders[(SYMBOL, ORDER["newClientOrderId"])]
        received_at = cached["received_at"]
        self.emit(order_event(stamp=self.stamp))
        self.assertIsNotNone(self.broker.order_event_receipt(ORDER))
        self.assertEqual(cached["received_at"], received_at)
        with patch("trading.ws_evidence.time.monotonic", return_value=received_at + 31):
            self.assertIsNone(self.broker.order_event_receipt(ORDER))

    def test_out_of_order_conflicts_and_quantity_regression_poison_order(self):
        pairs = [
            (order_event(stamp=self.stamp), order_event(stamp=self.stamp - 1)),
            (order_event(stamp=self.stamp), order_event(stamp=self.stamp, status="CANCELED", quantity="1")),
            (order_event(stamp=self.stamp - 1, status="PARTIALLY_FILLED", quantity="1.5"),
             order_event(stamp=self.stamp, status="PARTIALLY_FILLED", quantity="1")),
            (order_event(stamp=self.stamp - 1), order_event(stamp=self.stamp, status="NEW", quantity="0")),
        ]
        for first, second in pairs:
            with self.subTest(first=first, second=second):
                self.broker._private_evidence.reset(True)
                self.emit(first)
                self.emit(second)
                self.assertIsNone(self.broker.order_event_receipt(ORDER))
                self.emit(order_event(stamp=self.stamp + 1))
                self.assertIsNone(self.broker.order_event_receipt(ORDER))

    def test_cache_is_bounded_and_stale_receipts_expire(self):
        for index in range(PrivateEventEvidence.MAX_ORDERS + 8):
            self.emit(order_event(stamp=self.stamp, client_id="order-" + str(index)))
        self.assertEqual(len(self.broker._private_evidence.orders), PrivateEventEvidence.MAX_ORDERS)
        self.assertIsNone(self.broker.order_event_receipt({**ORDER, "newClientOrderId": "order-0"}))
        self.emit(order_event(stamp=self.stamp))
        with patch("trading.ws_evidence.time.time", return_value=self.stamp / 1000 + 31):
            self.assertIsNone(self.broker.order_event_receipt(ORDER))

    def test_disconnect_reconnect_and_old_stream_callbacks_cannot_restore_evidence(self):
        self.emit(order_event(stamp=self.stamp))
        checkpoint = self.broker.transfer_ws_checkpoint("100")
        old = self.stream
        old._set_connected(False)
        self.assertIsNone(self.broker.order_event_receipt(ORDER))
        self.assertIsNone(self.broker.transfer_ws_checkpoint("100"))
        old._set_connected(True)
        self.assertFalse(self.broker.transfer_ws_checkpoint_current(checkpoint))
        self.assertIsNone(self.broker.order_event_receipt(ORDER))
        self.broker.stop_cycle_hot_data()
        with patch.object(PrivateAccountStream, "start"):
            self.broker.start_cycle_hot_data([SYMBOL])
        self.stream = self.broker.cycle_stream
        self.stream._set_connected(True)
        self.emit(order_event(stamp=self.stamp))
        generation = self.broker._snapshot_generation
        old._on_state(False)
        old._on_state(True)
        old._on_event("ACCOUNT_UPDATE")
        old._on_payload(order_event(stamp=self.stamp - 1))
        self.assertEqual(self.broker._snapshot_generation, generation)
        self.assertIsNotNone(self.broker.order_event_receipt(ORDER))

    def test_checkpoint_and_evidence_are_json_safe_and_events_before_ack_are_accepted(self):
        checkpoint = self.broker.transfer_ws_checkpoint(Decimal("100"))
        json.dumps(checkpoint)
        created_at = (self.stamp - 1) / 1000
        self.emit(balance_event(stamp=self.stamp))
        # Signed HTTP completion still invalidates snapshots after this event.
        self.broker.invalidate_cycle_hot_data("账户写入结束")
        evidence, guard = self.broker.transfer_ws_balance(checkpoint, Fraction(-10), created_at)
        json.dumps(evidence)
        self.assertEqual(evidence["source"], "websocket")
        self.assertEqual(evidence["wallet"], "90")
        self.assertEqual(evidence["delta"], "-10")
        with self.broker._snapshot_lock:
            guard()
        self.broker.api.call.assert_not_called()

    def test_checkpoint_rejects_missing_wallet_and_pre_request_or_pre_checkpoint_event(self):
        for wallet in (None, "NaN", True, "-1"):
            self.assertIsNone(self.broker.transfer_ws_checkpoint(wallet))
        self.emit(balance_event(stamp=self.stamp))
        checkpoint = self.broker.transfer_ws_checkpoint("100")
        self.assertIsNone(self.broker.transfer_ws_balance(checkpoint, -10, (self.stamp - 1) / 1000))
        self.emit(balance_event(stamp=self.stamp + 1))
        self.assertIsNone(self.broker.transfer_ws_balance(checkpoint, -10, (self.stamp + 2) / 1000))

    def test_transfer_requires_exact_signed_delta_wallet_reason_asset_and_fields(self):
        wrong_events = [balance_event(stamp=self.stamp, delta="10"),
                        balance_event(stamp=self.stamp, wallet="91"),
                        balance_event(stamp=self.stamp, reason="FUNDING_FEE")]
        for field in ("bc", "wb"):
            event = balance_event(stamp=self.stamp)
            del event["a"]["B"][0][field]
            wrong_events.append(event)
        event = balance_event(stamp=self.stamp)
        event["a"]["B"][0]["a"] = "USDT"
        wrong_events.append(event)
        event = balance_event(stamp=self.stamp)
        event["a"]["B"].append(deepcopy(event["a"]["B"][0]))
        wrong_events.append(event)
        for event in wrong_events:
            with self.subTest(event=event):
                self.broker._private_evidence.reset(True)
                checkpoint = self.broker.transfer_ws_checkpoint("100")
                self.emit(event)
                self.assertIsNone(self.broker.transfer_ws_balance(checkpoint, -10, (self.stamp - 1) / 1000))

    def test_transfer_duplicates_conflicts_old_events_and_later_account_events_revoke_guard(self):
        for later in (balance_event(stamp=self.stamp), balance_event(stamp=self.stamp - 1),
                      balance_event(stamp=self.stamp, wallet="91"),
                      {"e": "ORDER_TRADE_UPDATE"}, {"e": "ACCOUNT_CONFIG_UPDATE"}):
            with self.subTest(later=later):
                self.broker._private_evidence.reset(True)
                checkpoint = self.broker.transfer_ws_checkpoint("100")
                self.emit(balance_event(stamp=self.stamp))
                _, guard = self.broker.transfer_ws_balance(checkpoint, -10, (self.stamp - 1) / 1000)
                self.emit(later)
                self.assertIsNone(self.broker.transfer_ws_balance(checkpoint, -10, (self.stamp - 1) / 1000))
                with self.assertRaises(TradingError):
                    guard()

    def test_transfer_guard_expires_on_time_write_disconnect_and_direct_account_event(self):
        for invalidation in (lambda: self.broker.invalidate_cycle_hot_data("写入"),
                             lambda: self.stream._set_connected(False),
                             lambda: self.broker._cycle_account_event("ACCOUNT_UPDATE")):
            self.stream._set_connected(False)
            self.stream._set_connected(True)
            checkpoint = self.broker.transfer_ws_checkpoint("100")
            self.emit(balance_event(stamp=self.stamp))
            _, guard = self.broker.transfer_ws_balance(checkpoint, -10, (self.stamp - 1) / 1000)
            invalidation()
            with self.assertRaises(SnapshotSuperseded):
                guard()
        self.stream._set_connected(False)
        self.stream._set_connected(True)
        checkpoint = self.broker.transfer_ws_checkpoint("100")
        self.emit(balance_event(stamp=self.stamp))
        _, guard = self.broker.transfer_ws_balance(checkpoint, -10, (self.stamp - 1) / 1000)
        with patch("trading.ws_evidence.time.time", return_value=self.stamp / 1000 + 31):
            with self.assertRaises(TradingError):
                guard()

    def test_transfer_guard_does_not_wait_for_stream_callback_lock(self):
        checkpoint = self.broker.transfer_ws_checkpoint("100")
        self.emit(balance_event(stamp=self.stamp))
        _, guard = self.broker.transfer_ws_balance(checkpoint, -10, (self.stamp - 1) / 1000)
        done = threading.Event()
        errors = []

        def verify():
            try:
                with self.broker._snapshot_lock:
                    guard()
            except Exception as exc:
                errors.append(exc)
            finally:
                done.set()

        with self.broker._cycle_stream_callback_lock:
            thread = threading.Thread(target=verify)
            thread.start()
            self.assertTrue(done.wait(1))
        thread.join(1)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
