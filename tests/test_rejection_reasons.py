"""Exchange reasons reach durable UI events without changing order recovery."""
import unittest
from unittest.mock import patch

import httpx

from trading.exchange import API, ExchangeError, RateBudget
from trading.exchange_messages import MISSING_REJECT_REASON, exchange_reason
from trading.execution import Executor
from trading.models import Plan, dec
from trading.paper import PaperBroker
from trading.store import Store
from .helpers import Fixture


class ExchangeReasonTests(unittest.TestCase):
    def test_only_bounded_plain_text_is_accepted(self):
        for value in (None, {}, [], 5018, False, "", "\r\n\t", "\x00\u202e"):
            with self.subTest(value=value):
                self.assertEqual(exchange_reason(value), "")
        self.assertEqual(exchange_reason("Order\r\nrejected\x00\u202e now"), "Order rejected now")
        self.assertEqual(exchange_reason("x" * 600), "x" * 499 + "…")
        self.assertEqual(exchange_reason("x" * 4097), "交易所原因过长，已省略")

    def test_signed_details_are_removed_before_truncation(self):
        secret = "never-display-this-value"
        for value in (f'signature={secret}', f'"private_key": "{secret}"',
                      f"API-KEY: {secret}", f"Authorization: Bearer {secret}",
                      f"token={secret}", f"https://example.invalid/order?x={secret}"):
            with self.subTest(value=value):
                self.assertNotIn(secret, exchange_reason("Rejected. " + value))
        for value in ("0x" + "ab" * 32, "cd" * 65, "0x" + "ef" * 20):
            self.assertNotIn(value, exchange_reason("Rejected for " + value))
        self.assertNotIn(secret, exchange_reason("x" * 480 + secret, secrets=[secret]))
        self.assertNotIn("abcd", exchange_reason("ABCD", secrets=[bytes.fromhex("abcd")]).lower())

    def api(self, response):
        api = API({"private_key": b"test-key", "user": "test-user", "signer": "test-signer"},
                  budget=RateBudget(), transport=httpx.MockTransport(lambda request: response))
        api.signed_parameters = lambda params: {**params, "signature": "test-signature"}
        self.addCleanup(api.close)
        return api

    def test_top_level_rejection_keeps_reason_and_error_metadata(self):
        for value, expected in (("Example rejection from exchange.", "Example rejection from exchange."),
                                (None, MISSING_REJECT_REASON), ({"secret": "hidden"}, MISSING_REJECT_REASON)):
            api = self.api(httpx.Response(400, json={"code": -5018, "msg": value}))
            with self.subTest(value=value), self.assertRaises(ExchangeError) as caught:
                api.call("POST", "/fapi/v3/order", signed=True)
            self.assertIn(expected, str(caught.exception))
            self.assertEqual((caught.exception.code, caught.exception.http_status, caught.exception.retry_after),
                             (-5018, 400, 0))

    def test_batch_and_top_level_remove_actual_signed_values(self):
        msg = "Rejected. test-user test-signer test-signature " + b"test-key".hex()
        for batch in (False, True):
            payload = {"code": -5018, "msg": msg}
            api = self.api(httpx.Response(200 if batch else 400, json=[payload] if batch else payload))
            with self.subTest(batch=batch):
                if batch:
                    result = str(api.call("POST", "/fapi/v3/batchOrders", signed=True))
                else:
                    with self.assertRaises(ExchangeError) as caught:
                        api.call("POST", "/fapi/v3/order", signed=True)
                    result = str(caught.exception)
                self.assertIn("Rejected.", result)
                for secret in ("test-user", "test-signer", "test-signature", b"test-key".hex()):
                    self.assertNotIn(secret, result)


class RejectionPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.executor = Executor(self.f.store, self.f.broker, self.f.market)

    def open(self):
        return self.executor.open_pair(self.f.account, self.f.broker.snapshot(["XAUUSD1"]), "XAUUSD1",
                                       Plan(dec("0.12")), self.f.market.book("XAUUSD1"))

    def test_reason_survives_restart_without_new_orders(self):
        rows = [{"code": -5018, "msg": "Example long rejection."},
                {"code": -2019, "msg": "Margin is insufficient."}]
        with patch.object(self.f.broker, "submit", return_value=rows) as submit, \
             patch.object(self.executor, "reconcile", return_value="simulated process stop"):
            self.open()
        submit.assert_called_once()
        store = Store(self.f.store.path)
        pending = store.intent("test")
        self.assertEqual([row["reject_reason"] for row in pending["receipts"].values()],
                         ["Example long rejection.", "Margin is insufficient."])
        broker = PaperBroker("test", self.f.market, store)
        with patch.object(broker, "submit", side_effect=AssertionError("must not resend")), \
             patch.object(broker, "query", side_effect=AssertionError("terminal receipts need no query")):
            Executor(store, broker, self.f.market).reconcile(self.f.account)
        self.assertIsNone(store.intent("test"))
        self.assertIn("多头 REJECTED（code=-5018）：Example long rejection.", store.events()[0]["message"])
        self.assertIn("空头 REJECTED（code=-2019）：Margin is insufficient.", store.events()[0]["message"])

    def test_missing_malformed_and_legacy_reasons_have_explicit_fallback(self):
        for value in (None, [], {}, "\x00\r\n"):
            with self.subTest(value=value), patch.object(self.f.broker, "submit", return_value=[
                    {"code": -5018, "msg": value}, {"code": -5018}]) as submit:
                self.open()
                submit.assert_called_once()
                self.assertIn(MISSING_REJECT_REASON, self.f.store.events()[0]["message"])
        row = {"status": "REJECTED", "reject_code": -5018}
        self.assertEqual(Executor.order_outcome({"positionSide": "LONG"}, row),
                         "多头 REJECTED（code=-5018）：" + MISSING_REJECT_REASON)

    def test_rejection_event_visible_even_before_compensation_finishes(self):
        original = self.f.broker.submit

        def submit(orders):
            if len(orders) == 2:
                return [original(orders[:1])[0], {"code": -5018, "msg": "Example short rejection."}]
            return [{"code": -2019, "msg": "Example repair rejection."}]

        with patch.object(self.f.broker, "submit", side_effect=submit) as send:
            self.open()
        self.assertEqual(send.call_count, 4)  # Existing bounded compensation attempts.
        self.assertEqual(self.f.store.intent("test")["status"], "attention")
        messages = " ".join(event["message"] for event in self.f.store.events())
        self.assertIn("Example short rejection.", messages)
        self.assertIn("Example repair rejection.", messages)
        self.assertNotIn("本批未形成新增双向仓位", messages)

    def test_top_level_reason_is_visible_while_existing_query_flow_continues(self):
        error = ExchangeError("Aster 拒绝请求（代码 -5018）：Example rejection.", code=-5018)
        with patch.object(self.f.broker, "submit", side_effect=error) as submit, \
             patch.object(self.f.broker, "query", side_effect=ExchangeError("not yet visible", code=-2013)):
            self.open()
            self.executor.reconcile(self.f.account)
        submit.assert_called_once()
        self.assertEqual(self.f.store.intent("test")["receipts"], {})
        self.assertIn("Example rejection.", self.f.store.events()[0]["message"])
        self.assertIsNone(self.executor.last_completed_intent)


if __name__ == "__main__":
    unittest.main()
