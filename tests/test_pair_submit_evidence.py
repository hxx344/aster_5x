"""Offline pair POST evidence: notional rejections never override uncertainty."""
from contextlib import ExitStack
from copy import deepcopy
import json
from unittest import TestCase
from unittest.mock import patch

import httpx

from tests import test_pair_notional_rejection as fixtures
from trading.exchange import API, AmbiguousOrder, BudgetWait, ExchangeError, RateBudget, RequestNotSent
from trading.pair_execution import PairTrader
from trading.store import Store


REJECTION = fixtures.ORIGINAL_REJECTION.replace("-2029", "-5018")


class PairSubmitEvidenceTests(TestCase):
    def fixture(self, kind="ordinary"):
        fixture = fixtures.PairNotionalRejectionTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        pair = fixture.store.pair("gold")
        pair["ordinary"]["enabled"] = kind == "ordinary"
        pair["cycle"]["enabled"] = kind == "cycle"
        fixture.store.save_pair(pair)
        return fixture

    def batch(self, fixture, state):
        return fixture.store.get("pair_batch:" + state["last_batch"]["id"])

    def test_both_rejected_orders_finish_after_positions_confirm_for_both_modes(self):
        for kind in ("ordinary", "cycle"):
            with self.subTest(kind=kind):
                f = self.fixture(kind)
                with ExitStack() as stack:
                    reads = stack.enter_context(patch.object(f.trader, "_read", wraps=f.trader._read))
                    sends = []
                    for broker in f.brokers.values():
                        sends.append(stack.enter_context(patch.object(broker, "submit", side_effect=ExchangeError(
                            REJECTION, code=-5018, http_status=400))))
                        stack.enter_context(patch.object(broker, "query", side_effect=AssertionError("known rejection")))
                    state = f.tick()
                self.assertIsNone(state["pending"], state)
                self.assertFalse(state["last_batch"]["completed"])
                self.assertEqual(reads.call_args.kwargs, {"reconciliation": True})
                self.assertEqual([send.call_count for send in sends], [1, 1])
                batch = self.batch(f, state)
                self.assertEqual(batch["kind"], kind)
                self.assertEqual(batch["repairs"], [])
                for leg in batch["legs"]:
                    self.assertEqual(leg["submit_evidence_version"], 2)
                    self.assertEqual(leg["submit_evidence"], {
                        "kind": "exchange_error", "code": -5018, "http_status": 400, "retry_after": 0})
                    self.assertEqual((leg["receipt"]["status"], leg["receipt"]["executedQty"],
                                      leg["receipt"]["reject_code"], leg["receipt"]["local_not_sent"]),
                                     ("REJECTED", "0", -5018, False))
                self.assertEqual(state["owned"], f.before)
                self.assertEqual(state["progress"]["quantities"], {"LONG": "0", "SHORT": "0"})
                self.assertTrue(all(not broker.state["orders"] for broker in f.brokers.values()))
                f.assert_baseline()

    def test_either_rejected_leg_reduces_only_successful_increment_in_both_modes(self):
        for kind in ("ordinary", "cycle"):
            for rejected in ("long", "short"):
                with self.subTest(kind=kind, rejected=rejected):
                    f = self.fixture(kind)
                    successful = "short" if rejected == "long" else "long"
                    broker = f.brokers[successful]
                    with patch.object(f.brokers[rejected], "submit", side_effect=ExchangeError(
                            REJECTION, code=-5018)) as denied, \
                            patch.object(f.brokers[rejected], "query", side_effect=AssertionError("known rejection")), \
                            patch.object(broker, "submit", wraps=broker.submit) as filled:
                        state = f.tick()
                        self.assertEqual(state["phase"], "repairing", state)
                        state = f.tick()
                    self.assertIsNone(state["pending"], state)
                    self.assertFalse(state["last_batch"]["completed"])
                    self.assertEqual(denied.call_count, 1)
                    orders = [call.args[0][0] for call in filled.call_args_list]
                    self.assertEqual([order["side"] for order in orders],
                                     ["SELL", "BUY"] if successful == "short" else ["BUY", "SELL"])
                    self.assertEqual(orders[0]["quantity"], orders[1]["quantity"])
                    batch = self.batch(f, state)
                    self.assertEqual(len(batch["repairs"]), 1)
                    self.assertEqual(batch["repairs"][0]["key"], successful)
                    self.assertEqual(batch["repairs"][0]["submit_evidence"], {"kind": "receipt"})
                    self.assertEqual(state["owned"], f.before)
                    f.assert_baseline()

    def test_single_structured_error_response_proves_rejection(self):
        f = self.fixture()
        with patch.object(f.brokers["long"], "submit", return_value=[{"code": -5018, "msg": "notional cap"}]), \
                patch.object(f.brokers["long"], "query", side_effect=AssertionError("known rejection")):
            state = f.tick()
        self.assertEqual(state["phase"], "repairing", state)
        leg = state["pending"]["legs"][0]
        self.assertEqual(leg["submit_evidence"], {"kind": "error_response", "code": -5018})
        self.assertEqual((leg["receipt"]["status"], leg["receipt"]["executedQty"], leg["receipt"]["reject_code"]),
                         ("REJECTED", "0", -5018))
        self.assertIsNone(f.tick()["pending"])
        f.assert_baseline()

    def test_ambiguous_same_text_and_code_stays_unknown_across_restart(self):
        f = self.fixture()
        with patch.object(f.brokers["long"], "submit", side_effect=AmbiguousOrder(
                REJECTION, code=-5018, http_status=400, retry_after=2)) as submit, \
                patch.object(f.brokers["long"], "query", side_effect=ExchangeError("not found", code=-2013)):
            state = f.tick()
            original = deepcopy(state["pending"]["legs"][0])
            self.assertEqual(original["submit_evidence"], {
                "kind": "ambiguous", "code": -5018, "http_status": 400, "retry_after": 2})
            persisted = Store(f.store.path).get("pair_runtime:gold")["pending"]["legs"][0]
            self.assertEqual(persisted, original)
            f.trader = PairTrader(f.engine)
            with patch.object(f.brokers["short"], "submit", side_effect=AssertionError("unknown leg blocks reduction")):
                for _ in range(2):
                    state = f.tick()
        self.assertEqual(submit.call_count, 1)
        self.assertEqual(state["phase"], "reconciling", state)
        self.assertIsNone(state["pending"]["legs"][0]["receipt"])
        self.assertEqual(state["pending"]["legs"][0]["submit_evidence"], original["submit_evidence"])
        self.assertEqual(state["pending"]["legs"][0]["order"], original["order"])
        self.assertEqual(state["pending"]["repairs"], [])

    def test_definitive_evidence_and_receipts_survive_delayed_position_read(self):
        f = self.fixture()
        read = f.trader._read
        def delayed_read(*args, **kwargs):
            if kwargs.get("reconciliation"):
                raise BudgetWait("position read waits", retry_after=20)
            return read(*args, **kwargs)
        with ExitStack() as stack:
            stack.enter_context(patch.object(f.trader, "_read", side_effect=delayed_read))
            for broker in f.brokers.values():
                stack.enter_context(patch.object(broker, "submit", side_effect=ExchangeError(REJECTION, code=-5018)))
            state = f.tick()
        self.assertIsNotNone(state["pending"])
        saved = Store(f.store.path).get("pair_runtime:gold")["pending"]["legs"]
        self.assertTrue(all(leg["receipt"]["status"] == "REJECTED" for leg in saved))
        self.assertTrue(all(leg["submit_evidence"]["kind"] == "exchange_error" for leg in saved))
        f.trader = PairTrader(f.engine)
        with ExitStack() as stack:
            for broker in f.brokers.values():
                stack.enter_context(patch.object(broker, "submit", side_effect=AssertionError("never resend")))
                stack.enter_context(patch.object(broker, "query", side_effect=AssertionError("saved terminal receipt")))
            state = f.tick()
        self.assertIsNone(state["pending"], state)
        self.assertEqual(self.batch(f, state)["legs"], saved)
        f.assert_baseline()

    def test_local_request_not_sent_retains_distinct_evidence(self):
        f = self.fixture()
        with ExitStack() as stack:
            for broker in f.brokers.values():
                stack.enter_context(patch.object(broker, "submit", side_effect=RequestNotSent(
                    REJECTION, code=-5018, retry_after=3)))
            state = f.tick()
        self.assertIsNone(state["pending"], state)
        for leg in self.batch(f, state)["legs"]:
            self.assertEqual(leg["submit_evidence"], {"kind": "request_not_sent", "code": -5018, "retry_after": 3})
            self.assertTrue(leg["receipt"]["local_not_sent"])
            self.assertNotIn("reject_code", leg["receipt"])
        f.assert_baseline()

    def test_unknown_error_code_cannot_be_classified_from_message(self):
        f = self.fixture()
        with patch.object(f.brokers["long"], "submit", side_effect=ExchangeError(REJECTION, code=-98765)), \
                patch.object(f.brokers["long"], "query", side_effect=ExchangeError("not found", code=-2013)):
            state = f.tick()
            f.trader = PairTrader(f.engine)
            state = f.tick()
        leg = state["pending"]["legs"][0]
        self.assertEqual(leg["submit_evidence"], {"kind": "exchange_error", "code": -98765, "retry_after": 0})
        self.assertIsNone(leg["receipt"])
        self.assertEqual(state["pending"]["repairs"], [])

    def test_invalid_structured_responses_do_not_prove_rejection(self):
        responses = ({"code": -5018}, [], [{"code": -5018}, {"code": -5018}],
                     [{"code": "-5018"}], [{"code": -5018.0}], [{"code": [-5018]}],
                     [{"code": -98765, "msg": REJECTION}])
        for response in responses:
            with self.subTest(response=response):
                f = self.fixture()
                with patch.object(f.brokers["long"], "submit", return_value=response), \
                        patch.object(f.brokers["long"], "query", side_effect=ExchangeError("not found", code=-2013)):
                    state = f.tick()
                    f.trader = PairTrader(f.engine)
                    state = f.tick()
                leg = state["pending"]["legs"][0]
                self.assertIsNone(leg["receipt"])
                self.assertIn(leg["submit_evidence"]["kind"], ("invalid", "error_response"))
                self.assertEqual(state["pending"]["repairs"], [])

    def test_http_server_error_or_timeout_overrides_notional_response_code(self):
        for status in (400, 503, 408):
            with self.subTest(http_status=status):
                f = self.fixture()
                requests = []
                def handle(request):
                    requests.append(request)
                    return httpx.Response(status, json={"code": -5018, "msg": "notional cap"})
                api = API(transport=httpx.MockTransport(handle), budget=RateBudget())
                self.addCleanup(api.close)
                def submit(orders):
                    return [api.call("POST", "/fapi/v3/order", orders[0], signed=True)]
                # Exercise real API response classification without credentials or network.
                with patch.object(api, "signed_parameters", side_effect=lambda params: params), \
                        patch.object(f.brokers["long"], "submit", side_effect=submit), \
                        patch.object(f.brokers["short"], "submit", side_effect=ExchangeError(REJECTION, code=-5018)), \
                        patch.object(f.brokers["long"], "query", side_effect=ExchangeError("not found", code=-2013)):
                    state = f.tick()
                    if status == 400:
                        self.assertIsNone(state["pending"], state)
                        leg = self.batch(f, state)["legs"][0]
                        self.assertEqual(leg["submit_evidence"], {
                            "kind": "exchange_error", "code": -5018, "http_status": 400, "retry_after": 0})
                    else:
                        f.trader = PairTrader(f.engine)
                        state = f.tick()
                        leg = state["pending"]["legs"][0]
                        self.assertEqual(leg["submit_evidence"], {
                            "kind": "ambiguous", "http_status": status, "retry_after": 0})
                        self.assertIsNone(leg["receipt"])
                        self.assertEqual(state["pending"]["repairs"], [])
                self.assertEqual(len(requests), 1)
                f.assert_baseline()

    def test_http_uncertainty_cannot_be_downgraded_by_generic_exchange_error(self):
        for status in (503, 408):
            with self.subTest(http_status=status):
                f = self.fixture()
                with patch.object(f.brokers["long"], "submit", side_effect=ExchangeError(
                        REJECTION, code=-5018, http_status=status)), \
                        patch.object(f.brokers["long"], "query", side_effect=ExchangeError("not found", code=-2013)):
                    state = f.tick()
                self.assertIsNone(state["pending"]["legs"][0]["receipt"])
                self.assertEqual(state["pending"]["repairs"], [])

    def test_version_is_durable_before_send_and_crash_cannot_prove_rejection(self):
        f = self.fixture()
        observed = []
        def crash(orders):
            saved = Store(f.store.path).get("pair_runtime:gold")
            observed.append(saved)
            self.assertTrue(all(leg["submit_evidence_version"] == 2 for leg in saved["pending"]["legs"]))
            self.assertTrue(all("submit_evidence" not in leg for leg in saved["pending"]["legs"]))
            raise SystemExit("simulated crash after entering submit")
        with ExitStack() as stack:
            for broker in f.brokers.values():
                stack.enter_context(patch.object(broker, "submit", side_effect=crash))
            with self.assertRaises(SystemExit):
                f.tick()
        self.assertEqual(len(observed), 2)
        f.trader = PairTrader(f.engine)
        with ExitStack() as stack:
            for broker in f.brokers.values():
                stack.enter_context(patch.object(broker, "submit", side_effect=AssertionError("never resend")))
                stack.enter_context(patch.object(broker, "query", side_effect=ExchangeError("not found", code=-2013)))
            state = f.tick()
        self.assertEqual(state["phase"], "reconciling", state)
        self.assertTrue(all(leg["receipt"] is None and "submit_evidence" not in leg for leg in state["pending"]["legs"]))
        self.assertEqual(state["pending"]["repairs"], [])

    def test_version_one_rejection_text_cannot_invoke_legacy_recovery(self):
        for message in (fixtures.ORIGINAL_REJECTION, REJECTION):
            with self.subTest(message=message):
                f = self.fixture()
                state = f.seed_legacy()
                state["pending"]["legs"][0].update(submit_evidence_version=1, submit_error=message)
                f.store.put("pair_runtime:gold", state)
                f.trader = PairTrader(f.engine)
                with patch.object(f.brokers["long"], "query", side_effect=ExchangeError("not found", code=-2013)), \
                        patch.object(f.brokers["short"], "submit", side_effect=AssertionError("unproven original result")):
                    state = f.tick()
                self.assertIsNone(state["pending"]["legs"][0]["receipt"])
                self.assertEqual(state["pending"]["repairs"], [])

    def test_evidence_contains_only_typed_safe_metadata(self):
        f = self.fixture()
        secret = "must-not-be-persisted-in-evidence"
        error = AmbiguousOrder(secret, code={"private_key": secret}, http_status=secret, retry_after=float("nan"))
        error.payload = {"signature": secret}
        with patch.object(f.brokers["long"], "submit", side_effect=error), \
                patch.object(f.brokers["long"], "query", side_effect=ExchangeError("not found", code=-2013)):
            state = f.tick()
        evidence = state["pending"]["legs"][0]["submit_evidence"]
        self.assertEqual(evidence, {"kind": "ambiguous"})
        self.assertNotIn(secret, json.dumps(evidence))
