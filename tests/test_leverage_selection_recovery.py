"""Higher-tier selection and live leverage rejection regressions; no live I/O."""
import copy
import json
import os
import time
import unittest
from unittest.mock import patch

import httpx

from trading.engine import Engine
from trading.exchange import API, ExchangeError, LeverageRejected, LiveBroker, RateBudget
from trading.execution import Executor
from trading.models import Book, SYMBOLS, dec
from trading.store import Store
from .helpers import Fixture
from .test_exchange_hardening import account_responses


XAU, SPCX, CL = SYMBOLS


class HigherLeverageSelectionTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.account["policy"].update(symbols=list(SYMBOLS), min_open_leverage=2)
        self.f.store.save_account(self.f.account)
        self.f.broker.state["leverages"] = dict.fromkeys(SYMBOLS, 2)
        self.f.broker.save()
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        for symbol in SYMBOLS:
            self.engine.poll_market(symbol)
            self.engine.markets[symbol]["capacities"] = {"2": "500000"}

    def seed_held_markets(self):
        quotes = {XAU: ("4999.995", "5000.005", "5000"),
                  SPCX: ("999.966", "1000.034", "1000"),
                  CL: ("99.98945", "100.01055", "100")}

        def quote(symbol):
            bid, ask, mark = map(dec, quotes[symbol])
            return Book(bid, ask, dec(50), dec(50), mark, time.time())

        patcher = patch.object(self.f.market, "book", side_effect=quote)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.f.broker.state["wallet"] = "100000"
        for symbol in SYMBOLS:
            for side in ("LONG", "SHORT"):
                self.f.broker.state["positions"][symbol + ":" + side] = {"qty": "5", "entry": quotes[symbol][2]}
        self.f.broker.save()
        snapshot = self.f.broker.snapshot(SYMBOLS)
        gross = {symbol: sum(p.qty * p.mark for p in snapshot.pair(symbol)) for symbol in SYMBOLS}
        self.assertEqual(gross, {XAU: dec(50000), SPCX: dec(10000), CL: dec(1000)})
        self.engine.rotation["test"] = 1

    def test_largest_held_xau_upgrades_confirms_adds_then_checks_next_tier(self):
        self.seed_held_markets()
        self.engine.markets[XAU]["capacities"].update({"4": "1984153", "5": "1984153"})
        with patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as change, \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.engine.tick_account("test")
            change.assert_called_once_with(XAU, 4)
            submit.assert_not_called()
            self.engine.tick_account("test")
            self.assertIsNone(self.f.store.intent("test"))
            submit.assert_not_called()
            self.engine.tick_account("test")
            submit.assert_called_once()
            self.assertEqual({o["symbol"] for o in submit.call_args.args[0]}, {XAU})
            self.assertEqual(change.call_count, 1)
            self.engine.tick_account("test")
            self.assertEqual(change.call_args.args, (XAU, 5))
            self.assertEqual(change.call_count, 2)
        self.assertTrue(self.f.store.account("test")["enabled"])

    def test_largest_held_xau_skips_unavailable_four_and_upgrades_before_spcx(self):
        self.seed_held_markets()
        self.engine.markets[XAU]["capacities"].update({"4": "0", "5": "1984153"})
        with patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as change, \
             patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.engine.tick_account("test")
            change.assert_called_once_with(XAU, 5)
            submit.assert_not_called()

    def test_largest_held_xau_at_five_receives_next_add_before_spcx(self):
        self.seed_held_markets()
        self.f.broker.state["leverages"][XAU] = 5
        self.f.broker.save()
        self.engine.markets[XAU]["capacities"]["5"] = "1984153"
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.engine.tick_account("test")
        submit.assert_called_once()
        self.assertEqual({o["symbol"] for o in submit.call_args.args[0]}, {XAU})
        self.assertTrue(self.f.store.account("test")["enabled"])

    def test_flat_current_capacity_does_not_hide_available_higher_tier(self):
        self.engine.markets[XAU]["capacities"].update({"4": "500000", "5": "500000"})
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.engine.tick_account("test")
        intent = self.f.store.intent("test")
        self.assertIsNotNone(intent)
        self.assertEqual((intent["symbol"], intent["target"]), (XAU, 4))
        submit.assert_not_called()

    def test_xau_five_precedes_spcx_two_even_when_rotation_starts_at_spcx(self):
        self.f.broker.state["leverages"][XAU] = 5
        self.f.broker.save()
        self.engine.markets[XAU]["capacities"]["5"] = "1984153"
        self.engine.rotation["test"] = 1
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.engine.tick_account("test")
        submit.assert_called_once()
        self.assertEqual({o["symbol"] for o in submit.call_args.args[0]}, {XAU})

    def test_available_upgrade_precedes_lower_current_tier(self):
        self.engine.markets[XAU]["capacities"]["5"] = "1984153"
        self.engine.rotation["test"] = 1
        with patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as change:
            self.engine.tick_account("test")
        change.assert_called_once_with(XAU, 5)
        self.assertEqual(self.f.broker.state["orders"], {})

    def test_flat_upgrade_is_included_in_scheduler_cost(self):
        row = {**self.f.account, "mode": "live"}
        self.engine.view("test", snapshot={"positions": [
            {"symbol": symbol, "leverage": 2, "qty": "0", "side": side}
            for symbol in SYMBOLS for side in ("LONG", "SHORT")]})
        before = self.engine.scheduling([row])["test"]["gap"]
        self.engine.markets[XAU]["capacities"]["4"] = "500000"
        self.assertGreater(self.engine.scheduling([row])["test"]["gap"], before)


class LiveLeverageRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.responses = account_responses()
        for rows in (self.responses["/fapi/v3/positionRisk"],
                     self.responses["/fapi/v3/accountWithJoinMargin"]["positions"]):
            for row in rows:
                row.update(leverage="2", positionAmt="0", entryPrice="0")
        self.result = {"symbol": XAU, "leverage": 4}
        self.http_status = 200
        self.writes = []

        def handle(request):
            if request.method == "POST":
                self.writes.append(request.url.path)
                if isinstance(self.result, Exception):
                    raise self.result
                if isinstance(self.result, httpx.Response):
                    return self.result
                return httpx.Response(self.http_status, json=self.result)
            return httpx.Response(200, json=copy.deepcopy(self.responses[request.url.path]))

        self.api = API(transport=httpx.MockTransport(handle), budget=RateBudget())
        self.api.signed_parameters = lambda params: params
        self.addCleanup(self.api.close)
        self.broker = LiveBroker({}, self.f.market, api=self.api)
        self.executor = Executor(self.f.store, self.broker, self.f.market)

    def begin(self):
        try:
            return self.executor.leverage(self.f.account, XAU, 2, 4)
        except ExchangeError as exc:
            return str(exc)

    def engine_at_risk_limit(self):
        self.f.account["mode"] = "live"
        self.f.account["policy"]["min_open_leverage"] = 2
        self.f.store.save_account(self.f.account)
        for rows in (self.responses["/fapi/v3/positionRisk"],
                     self.responses["/fapi/v3/accountWithJoinMargin"]["positions"]):
            for row in rows:
                row.update(positionAmt="250" if row["positionSide"] == "LONG" else "-250",
                           entryPrice="100", markPrice="100", leverage="2")
        self.responses["/fapi/v3/accountWithJoinMargin"]["assets"][0].update(
            crossWalletBalance="50000", crossUnPnl="0", availableBalance="25000", maintMargin="1250")
        patcher = patch.object(self.f.market, "book", side_effect=lambda symbol: Book(
            dec("99.9999"), dec("100.0001"), dec(50), dec(50), dec(100), time.time()))
        patcher.start()
        self.addCleanup(patcher.stop)
        engine = Engine(self.f.store, market=self.f.market)
        engine.brokers["test"] = self.broker
        engine.poll_market(XAU)
        engine.markets[XAU]["capacities"] = {"2": "11178592", "4": "1984153", "5": "1984153"}
        self.assertEqual(self.broker.snapshot([XAU]).ratio, dec(".5"))
        return engine

    def test_documented_http_rejections_and_rate_codes_end_leverage_intent(self):
        cases = ((429, None, 240), (418, None, 86400), (403, None, 240),
                 (200, -1003, 180), (200, -1015, 180))
        for status, code, delay in cases:
            with self.subTest(status=status, code=code):
                previous = self.f.store.intent("test")
                if previous:
                    previous["status"] = "aborted"
                    self.f.store.save_intent(previous)
                self.api.budget = RateBudget()
                self.result = (httpx.Response(status, json={"code": code, "msg": "private server details"}) if code else
                               httpx.Response(status, text="private gateway page", headers={"Retry-After": "240"}))
                with self.assertRaises(LeverageRejected) as raised:
                    self.executor.leverage(self.f.account, XAU, 2, 4)
                self.assertIsNone(self.f.store.intent("test"))
                self.assertTrue(self.f.store.account("test")["enabled"])
                self.assertEqual(raised.exception.http_status, status)
                self.assertGreaterEqual(raised.exception.retry_after, delay)
                self.assertNotIn("private", str(raised.exception))
                self.assertIn(str(code if code else status), str(raised.exception))
                with self.f.store.connect() as db:
                    saved = json.loads(db.execute("SELECT data FROM intents ORDER BY rowid DESC LIMIT 1").fetchone()[0])
                self.assertEqual(saved["status"], "aborted")
                self.assertEqual(saved["submission_http_status"], status)

    def test_unknown_http_outcomes_keep_pending_with_status_for_diagnostics(self):
        for status, code in ((400, -1007), (403, -1007), (408, -2027), (500, -2027), (503, -2027)):
            with self.subTest(status=status, code=code):
                self.api.budget = RateBudget()
                previous = self.f.store.intent("test")
                if previous:
                    previous["status"] = "aborted"
                    self.f.store.save_intent(previous)
                self.result = httpx.Response(status, json={"code": code})
                self.begin()
                pending = self.f.store.intent("test")
                self.assertIsNotNone(pending)
                self.assertEqual(pending.get("submission_http_status"), status)

    def test_rate_limit_at_risk_limit_replans_after_backoff_and_confirms(self):
        engine = self.engine_at_risk_limit()
        self.result = httpx.Response(429, headers={"Retry-After": "240"})
        with patch.dict(os.environ, {"ASTER_ALLOW_LIVE": "1"}):
            self.assertGreaterEqual(engine.tick_account("test"), 240)
            self.assertIsNone(self.f.store.intent("test"))
            self.assertTrue(self.f.store.account("test")["enabled"])
            engine.tick_account("test")
            self.assertEqual(self.writes, ["/fapi/v3/leverage"])
            self.result = {"symbol": XAU, "leverage": 4}
            with patch("trading.exchange.time.monotonic", return_value=self.api.budget.until + 1):
                engine.tick_account("test")
                self.assertEqual(self.f.store.intent("test")["target"], 4)
                engine.tick_account("test")
                self.assertIsNotNone(self.f.store.intent("test"))
                for rows in (self.responses["/fapi/v3/positionRisk"],
                             self.responses["/fapi/v3/accountWithJoinMargin"]["positions"]):
                    for row in rows:
                        row["leverage"] = "4"
                self.responses["/fapi/v3/accountWithJoinMargin"]["assets"][0]["availableBalance"] = "37500"
                engine.tick_account("test")
                self.assertEqual(self.broker.snapshot([XAU]).ratio, dec(".25"))
            self.assertIsNone(self.f.store.intent("test"))
            self.assertTrue(self.f.store.account("test")["enabled"])
            self.assertEqual(self.writes, ["/fapi/v3/leverage"] * 2)

    def test_unknown_at_risk_limit_recheck_does_not_resend_or_resume(self):
        engine = self.engine_at_risk_limit()
        self.result = httpx.Response(503)
        with patch.dict(os.environ, {"ASTER_ALLOW_LIVE": "1"}):
            engine.tick_account("test")
            pending = self.f.store.intent("test")
            pending["created_at"] = time.time() - 121
            self.f.store.save_intent(pending)
            engine.tick_account("test")
            engine.retry("test")
            engine.tick_account("test")
            self.assertEqual(self.f.store.intent("test")["status"], "attention")
            self.assertIn("503", self.f.store.intent("test")["last_error"])
            self.assertFalse(self.f.store.account("test")["enabled"])
            for rows in (self.responses["/fapi/v3/positionRisk"],
                         self.responses["/fapi/v3/accountWithJoinMargin"]["positions"]):
                for row in rows:
                    row["leverage"] = "4"
            engine.tick_account("test")
            self.assertIsNone(self.f.store.intent("test"))
            self.assertFalse(self.f.store.account("test")["enabled"])
            self.assertEqual(self.writes, ["/fapi/v3/leverage"])

    def test_definite_rejection_does_not_wait_forever_for_an_unapplied_change(self):
        for code in (-2027, -2028, -4028, -1022):
            with self.subTest(code=code):
                self.result = {"code": code, "msg": "private exchange payload"}
                self.http_status = 400
                before = len(self.writes)
                message = self.begin()
                self.assertIsNone(self.f.store.intent("test"))
                self.assertIn(XAU, message)
                self.assertIn(str(code), message)
                self.assertNotIn("private", message)
                self.assertEqual(self.writes[before:], ["/fapi/v3/leverage"])
                self.assertTrue(self.f.store.account("test")["enabled"])

    def test_rejected_change_can_be_replanned_after_fresh_account_checks(self):
        self.result = {"code": -2027}
        self.begin()
        self.assertIsNone(self.f.store.intent("test"))
        self.result = {"symbol": XAU, "leverage": 4}
        self.begin()
        self.assertIsNotNone(self.f.store.intent("test"))
        for rows in (self.responses["/fapi/v3/positionRisk"],
                     self.responses["/fapi/v3/accountWithJoinMargin"]["positions"]):
            for row in rows:
                row["leverage"] = "4"
        self.executor.reconcile(self.f.account)
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.f.store.get("open_after_leverage:test:XAUUSD1"), 4)
        self.assertEqual(len(self.writes), 2)

    def test_legacy_pending_two_to_four_or_five_confirms_actual_five_without_resending(self):
        self.f.account.update(enabled=False, pause_reason="旧版本未确认")
        self.f.store.save_account(self.f.account)
        for rows in (self.responses["/fapi/v3/positionRisk"],
                     self.responses["/fapi/v3/accountWithJoinMargin"]["positions"]):
            for row in rows:
                row["leverage"] = "5"
        for target in (4, 5):
            with self.subTest(target=target):
                self.f.store.save_intent({"id": f"legacy-{target}", "kind": "leverage", "account_id": "test",
                    "symbol": XAU, "previous": 2, "target": target, "created_at": time.time() - 3600,
                    "status": "attention", "last_error": "杠杆变更尚未确认，请核对账户后重新检查"})
                self.assertEqual(self.executor.reconcile(self.f.account), "杠杆调整已确认")
                self.assertIsNone(self.f.store.intent("test"))
                self.assertEqual(self.f.store.get("open_after_leverage:test:XAUUSD1"), 5)
                self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.writes, [])

    def test_legacy_pending_with_fifty_thousand_held_confirms_actual_five(self):
        self.f.account.update(enabled=False, pause_reason="旧版本未确认")
        self.f.store.save_account(self.f.account)
        for rows in (self.responses["/fapi/v3/positionRisk"],
                     self.responses["/fapi/v3/accountWithJoinMargin"]["positions"]):
            for row in rows:
                row.update(leverage="5", positionAmt="250" if row["positionSide"] == "LONG" else "-250",
                           entryPrice="100", markPrice="100")
        self.responses["/fapi/v3/accountWithJoinMargin"]["assets"][0].update(
            crossWalletBalance="100000", availableBalance="90000", maintMargin="1250")
        snapshot = self.broker.snapshot([XAU])
        self.assertEqual(sum(p.qty * p.mark for p in snapshot.pair(XAU)), dec(50000))
        for target in (4, 5):
            with self.subTest(target=target):
                self.f.store.save_intent({"id": f"held-legacy-{target}", "kind": "leverage", "account_id": "test",
                    "symbol": XAU, "previous": 2, "target": target, "created_at": time.time() - 3600,
                    "status": "attention", "last_error": "杠杆变更尚未确认，请核对账户后重新检查"})
                self.assertEqual(self.executor.reconcile(self.f.account), "杠杆调整已确认")
                self.assertIsNone(self.f.store.intent("test"))
                self.assertFalse(self.f.store.account("test")["enabled"])
        self.assertEqual(self.writes, [])

    def test_malformed_acknowledgement_keeps_pending_until_account_confirmation(self):
        for response in (None, [], {}, {"symbol": SPCX, "leverage": 4},
                         {"symbol": XAU, "leverage": True}, {"symbol": XAU, "leverage": 3}):
            with self.subTest(response=response):
                previous = self.f.store.intent("test")
                if previous:
                    previous["status"] = "aborted"
                    self.f.store.save_intent(previous)
                self.result = response
                self.begin()
                intent = self.f.store.intent("test")
                self.assertIsNotNone(intent)
                self.assertNotIn("change_response", intent)
                self.assertTrue(intent.get("submission_error"))
                self.executor.reconcile(self.f.account)
                self.assertIsNotNone(self.f.store.intent("test"))

    def test_success_response_is_saved_but_stale_actual_leverage_cannot_confirm(self):
        self.begin()
        intent = self.f.store.intent("test")
        self.assertEqual(intent.get("change_response"), {"symbol": XAU, "leverage": 4})
        self.executor.reconcile(self.f.account)
        self.assertIsNotNone(self.f.store.intent("test"))
        for rows in (self.responses["/fapi/v3/positionRisk"],
                     self.responses["/fapi/v3/accountWithJoinMargin"]["positions"]):
            for row in rows:
                row["leverage"] = "5"
        self.executor.reconcile(self.f.account)
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.f.store.get("open_after_leverage:test:XAUUSD1"), 5)
        self.assertEqual(len(self.writes), 1)

    def test_unknown_result_retains_original_error_and_never_resubmits(self):
        self.result = {"code": -1007, "msg": "private timeout details"}
        self.begin()
        intent = self.f.store.intent("test")
        intent["created_at"] = time.time() - 121
        self.f.store.save_intent(intent)
        restored = Executor(Store(self.f.store.path), self.broker, self.f.market)
        reason = restored.reconcile(self.f.account)
        self.assertIn(XAU, reason)
        self.assertIn("4x", reason)
        self.assertIn("2x", reason)
        self.assertIn("-1007", reason)
        self.assertNotIn("private", reason)
        restored.reconcile(self.f.account)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(self.f.store.intent("test").get("submission_code"), -1007)

    def test_unknown_errors_and_http_failures_cannot_be_treated_as_rejected(self):
        for status, result in ((200, {"code": -1000}), (200, {"code": -1001}),
                               (200, {"code": -1006}), (200, {"code": -9999}),
                               (500, {"code": -2027}), (408, {"code": -2027})):
            with self.subTest(status=status, result=result):
                previous = self.f.store.intent("test")
                if previous:
                    previous["status"] = "aborted"
                    self.f.store.save_intent(previous)
                self.http_status, self.result = status, result
                self.begin()
                self.assertIsNotNone(self.f.store.intent("test"))


if __name__ == "__main__":
    unittest.main()
