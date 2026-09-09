"""Adversarial exchange fixtures only; these tests never reach a real exchange."""
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from fractions import Fraction
import threading
import unittest
from unittest.mock import Mock, patch

from eth_account import Account
import httpx

import monitor
from trading.engine import DEFAULT_POLICY
from trading.exchange import API, AmbiguousOrder, ExchangeError, LiveBroker, MarketData, RateBudget
from trading.models import AccountModeError, TradingError, dec, plan_pair
from trading.paper import DemoMarket, PAPER_BRACKETS


class FixtureAPI:
    def __init__(self, responses):
        self.responses, self.calls = responses, []

    def call(self, method, path, *args, **kwargs):
        self.calls.append((method, path, args, kwargs))
        result = self.responses[path]
        if isinstance(result, Exception):
            raise result
        return copy.deepcopy(result)


def account_responses():
    positions = [{"symbol": "XAUUSD1", "positionSide": side, "positionAmt": "1", "entryPrice": "100",
                  "markPrice": "100", "leverage": "4", "unRealizedProfit": "0", "liquidationPrice": "0",
                  "marginType": "cross"} for side in ("LONG", "SHORT")]
    return {
        "/fapi/v3/positionSide/dual": {"dualSidePosition": True},
        "/fapi/v3/multiAssetsMargin": {"multiAssetsMargin": False},
        "/fapi/v3/accountWithJoinMargin": {"canTrade": True,
            "assets": [{"asset": "USD1", "crossWalletBalance": "200", "crossUnPnl": "0",
                        "maintMargin": "5", "availableBalance": "150"}],
            "positions": [{**p, "isolated": False} for p in positions]},
        "/fapi/v3/positionRisk": positions,
        "/fapi/v3/openOrders": [],
        "/fapi/v3/leverageBracket": {"symbol": "XAUUSD1", "brackets": PAPER_BRACKETS},
        "/fapi/v3/commissionRate": {"symbol": "XAUUSD1", "takerCommissionRate": "0.0004"},
    }


class SnapshotHardeningTests(unittest.TestCase):
    def setUp(self):
        self.responses = account_responses()
        self.api = FixtureAPI(self.responses)
        self.market = DemoMarket()
        self.broker = LiveBroker({}, self.market, api=self.api)

    def snapshot(self):
        return self.broker.snapshot(["XAUUSD1"])

    def test_duplicate_account_row_cannot_hide_unrepresented_exposure(self):
        positions = self.responses["/fapi/v3/accountWithJoinMargin"]["positions"]
        positions.extend([{**positions[0], "symbol": "SPCXUSD1", "positionAmt": amount} for amount in ("10", "0")])
        with self.assertRaisesRegex(TradingError, "重复记录"):
            self.snapshot()
        self.assertTrue(all(call[0] == "GET" for call in self.api.calls))

    def test_duplicate_risk_rows_are_rejected_even_when_identical(self):
        rows = self.responses["/fapi/v3/positionRisk"]
        rows.append(copy.deepcopy(rows[0]))
        with self.assertRaisesRegex(TradingError, "重复记录"):
            self.snapshot()

    def test_duplicate_usd1_balance_cannot_choose_favorable_equity(self):
        assets = self.responses["/fapi/v3/accountWithJoinMargin"]["assets"]
        assets.insert(0, {**assets[0], "crossWalletBalance": "999999"})
        with self.assertRaisesRegex(TradingError, "资产重复"):
            self.snapshot()

    def test_fractional_boolean_or_out_of_range_leverage_is_not_coerced(self):
        for source in ("account", "risk"):
            for value in (4.5, "4.5", True, False, "0", "126", "NaN", "Infinity", None):
                with self.subTest(source=source, value=value):
                    responses = account_responses()
                    rows = responses["/fapi/v3/accountWithJoinMargin"]["positions"] if source == "account" else responses["/fapi/v3/positionRisk"]
                    rows[0]["leverage"] = value
                    with self.assertRaises(TradingError):
                        LiveBroker({}, self.market, api=FixtureAPI(responses)).snapshot(["XAUUSD1"])

    def test_valid_integer_leverage_and_external_positions_keep_gross_margin(self):
        for path, rows in (("risk", self.responses["/fapi/v3/positionRisk"]),
                           ("account", self.responses["/fapi/v3/accountWithJoinMargin"]["positions"])):
            rows[0]["leverage"] = "4.0"
            rows[1]["positionAmt"] = "-1"
            rows.append({**rows[0], "symbol": "SPCXUSD1", "positionAmt": "2", "leverage": "10"})
        result = self.snapshot()
        self.assertEqual(result.occupied_margin, dec("70"))
        self.assertEqual(result.ratio, dec("0.35"))

    def test_newer_position_loss_prevents_stale_equity_crossing_half_margin(self):
        account = self.responses["/fapi/v3/accountWithJoinMargin"]
        account["assets"][0].update(crossWalletBalance="10004.3064", crossUnPnl="0", availableBalance="5500")
        account["positions"][1]["positionAmt"] = "0.999"
        rows = self.responses["/fapi/v3/positionRisk"]
        rows[0].update(entryPrice="10000", markPrice="9000", unRealizedProfit="-1000")
        rows[1].update(entryPrice="10000", markPrice="9000", positionAmt="0.999", unRealizedProfit="999")
        snapshot = self.snapshot()
        self.assertEqual(snapshot.wallet, dec("10004.3064"))
        self.assertEqual(snapshot.unrealized, dec("-1"))
        self.assertEqual(snapshot.equity, snapshot.wallet + snapshot.unrealized)
        self.assertEqual(snapshot.available, dec("5499"))
        book = replace(self.market.book("XAUUSD1"), bid=dec(9000), ask=dec(9000), mark=dec(9000))
        plan = plan_pair(snapshot, book, self.market.rules["XAUUSD1"], {4: dec(500000)}, {**DEFAULT_POLICY, "order_notional": "100000"})
        self.assertEqual(plan.qty, dec(".111"))

        def realized_ratio(qty):
            equity = snapshot.wallet + sum(p.unrealized for p in snapshot.positions) - qty * dec("18000") * dec(".0004")
            occupied = snapshot.occupied_margin + 2 * qty * dec(9000) / 4
            return occupied / equity

        self.assertEqual(plan.projected_ratio, realized_ratio(plan.qty))
        self.assertLessEqual(realized_ratio(plan.qty), dec(".5"))
        self.assertGreater(realized_ratio(plan.qty + dec(".001")), dec(".5"))
        # Without the loss adjustment this next lot appeared to fit exactly at 50%.
        old_equity = snapshot.wallet - dec(".112") * dec(18000) * dec(".0004")
        self.assertEqual((snapshot.occupied_margin + dec(".112") * dec(4500)) / old_equity, dec(".5"))

    def test_newer_position_gain_does_not_increase_equity_or_available_cash(self):
        account = self.responses["/fapi/v3/accountWithJoinMargin"]
        for reported, positions_pnl in (("0", "20"), ("-10", "0"), ("5", "20"), ("10", "-20")):
            with self.subTest(reported=reported, positions_pnl=positions_pnl):
                account["assets"][0].update(crossUnPnl=reported, availableBalance="1")
                self.responses["/fapi/v3/positionRisk"][0].update(unRealizedProfit=positions_pnl,
                    entryPrice=str(dec(100) - dec(positions_pnl)))
                snapshot = self.snapshot()
                selected = min(dec(reported), dec(positions_pnl))
                self.assertEqual(snapshot.unrealized, selected)
                self.assertEqual(snapshot.equity, snapshot.wallet + selected)
                self.assertEqual(snapshot.available, dec(1) - (dec(reported) - selected))
                self.assertLessEqual(snapshot.equity, snapshot.wallet + dec(reported))
                self.assertLessEqual(snapshot.available, 1)

    def test_mark_entry_loss_is_charged_when_reported_position_pnl_lags(self):
        account = self.responses["/fapi/v3/accountWithJoinMargin"]
        account["assets"][0].update(crossUnPnl="0", availableBalance="150")
        account["positions"][1]["positionAmt"] = "0.999"
        rows = self.responses["/fapi/v3/positionRisk"]
        rows[0].update(markPrice="90", unRealizedProfit="0")
        rows[1].update(markPrice="90", positionAmt="0.999", unRealizedProfit="0")
        snapshot = self.snapshot()
        self.assertEqual(sum(p.unrealized for p in snapshot.positions), 0)
        self.assertEqual(snapshot.unrealized, dec("-.01"))
        self.assertEqual(snapshot.equity, dec("199.99"))
        self.assertEqual(snapshot.available, dec("149.99"))
        self.assertEqual(snapshot.equity, snapshot.wallet + snapshot.unrealized)

    def test_nonzero_position_requires_positive_entry_price(self):
        for entry in ("0", "-1", None, "NaN"):
            with self.subTest(entry=entry):
                self.responses["/fapi/v3/positionRisk"][0]["entryPrice"] = entry
                with self.assertRaises(TradingError):
                    self.snapshot()

    def test_reported_position_loss_is_not_replaced_by_a_favorable_mark(self):
        self.responses["/fapi/v3/positionRisk"][0]["unRealizedProfit"] = "-20"
        snapshot = self.snapshot()
        self.assertTrue(all(p.mark == p.entry for p in snapshot.positions))
        self.assertEqual(snapshot.unrealized, dec(-20))
        self.assertEqual(snapshot.equity, dec(180))
        self.assertEqual(snapshot.available, dec(130))

    def test_high_precision_small_loss_keeps_exact_equity_cash_and_quantities(self):
        qty = "1.000000000000000000000000000001"
        mark = "99.99999999999999999999999999999"
        wallet = "200.000000000000000000000000000001"
        available = "150.000000000000000000000000000001"
        account = self.responses["/fapi/v3/accountWithJoinMargin"]
        account["assets"][0].update(crossWalletBalance=wallet, availableBalance=available)
        for rows in (account["positions"], self.responses["/fapi/v3/positionRisk"]):
            rows[0]["positionAmt"], rows[1]["positionAmt"] = qty, "-" + qty
        self.responses["/fapi/v3/positionRisk"][0]["markPrice"] = mark
        snapshot = self.snapshot()
        exact_pnl = Fraction(qty) * (Fraction(mark) - 100)
        self.assertLess(exact_pnl, 0)
        self.assertEqual(Fraction(snapshot.unrealized), exact_pnl)
        self.assertEqual(Fraction(snapshot.equity), Fraction(wallet) + exact_pnl)
        self.assertEqual(Fraction(snapshot.available), Fraction(available) + exact_pnl)
        self.assertEqual(Fraction(snapshot.equity), Fraction(snapshot.wallet) + Fraction(snapshot.unrealized))
        self.assertEqual(tuple(p.qty for p in snapshot.positions), (dec(qty), dec(qty)))

    def test_sub_precision_quantity_mismatch_is_not_rounded_into_agreement(self):
        self.responses["/fapi/v3/positionRisk"][0]["positionAmt"] = "1.0000000000000000000000000001"
        with self.assertRaisesRegex(TradingError, "余额与持仓快照正在同步"):
            self.snapshot()

    def test_loss_from_unconfigured_usd1_position_is_included_in_equity(self):
        for rows in (self.responses["/fapi/v3/positionRisk"], self.responses["/fapi/v3/accountWithJoinMargin"]["positions"]):
            rows.append({**rows[0], "symbol": "SPCXUSD1", "positionAmt": "2", "entryPrice": "110",
                         "markPrice": "100", "unRealizedProfit": "-20", "leverage": "10"})
        snapshot = self.snapshot()
        self.assertEqual(snapshot.unrealized, dec(-20))
        self.assertEqual(snapshot.equity, dec(180))
        self.assertEqual(snapshot.available, dec(130))
        self.assertEqual(snapshot.occupied_margin, dec(70))

    def test_nonzero_both_position_requires_mode_attention(self):
        rows = self.responses["/fapi/v3/positionRisk"]
        rows.append({**rows[0], "symbol": "SPCXUSD1", "positionSide": "BOTH"})
        with self.assertRaises(AccountModeError):
            self.snapshot()

    def test_invalid_position_rows_fail_with_a_sanitized_error(self):
        for path in ("/fapi/v3/positionRisk", "/fapi/v3/accountWithJoinMargin"):
            for malformed in (None, {}, [None], [{"symbol": "secret-payload", "positionSide": "invalid"}],
                              [{"symbol": [], "positionSide": "LONG", "positionAmt": "1"}]):
                with self.subTest(path=path, malformed=malformed):
                    responses = account_responses()
                    if path.endswith("JoinMargin"):
                        responses[path]["positions"] = malformed
                    else:
                        responses[path] = malformed
                    with self.assertRaises(TradingError) as caught:
                        LiveBroker({}, self.market, api=FixtureAPI(responses)).snapshot(["XAUUSD1"])
                    self.assertNotIn("secret-payload", str(caught.exception))

    def test_malformed_account_and_modes_fail_closed(self):
        cases = (("/fapi/v3/positionSide/dual", []), ("/fapi/v3/multiAssetsMargin", None),
                 ("/fapi/v3/accountWithJoinMargin", []))
        for path, value in cases:
            with self.subTest(path=path):
                responses = account_responses()
                responses[path] = value
                with self.assertRaises(TradingError):
                    LiveBroker({}, self.market, api=FixtureAPI(responses)).snapshot(["XAUUSD1"])

    def test_flat_missing_rows_share_one_mark_request_per_symbol(self):
        self.responses["/fapi/v3/positionRisk"] = []
        for row in self.responses["/fapi/v3/accountWithJoinMargin"]["positions"]:
            row["positionAmt"] = "0"
        self.market.book = Mock(wraps=self.market.book)
        result = self.snapshot()
        self.assertEqual(result.occupied_margin, 0)
        self.assertEqual(len(result.positions), 2)
        self.market.book.assert_called_once_with("XAUUSD1")
        self.assertEqual(self.responses["/fapi/v3/positionRisk"], [])

    def test_snapshot_uses_a_fixed_fee_estimate_without_a_commission_response(self):
        self.responses.pop("/fapi/v3/commissionRate")
        snapshot = self.snapshot()
        self.assertEqual(snapshot.fees, {"XAUUSD1": dec("0.0004")})
        self.assertIsNone(snapshot.open_orders)

    def test_brackets_must_identify_one_unambiguous_requested_symbol(self):
        valid = self.responses["/fapi/v3/leverageBracket"]
        for value in (None, [], [None], [valid, valid], {"symbol": "CLUSD1", "brackets": PAPER_BRACKETS}):
            with self.subTest(value=value):
                responses = account_responses()
                responses["/fapi/v3/leverageBracket"] = value
                with self.assertRaises(TradingError):
                    LiveBroker({}, self.market, api=FixtureAPI(responses)).snapshot(["XAUUSD1"])
        self.responses["/fapi/v3/leverageBracket"] = [valid]
        self.assertEqual(self.snapshot().brackets["XAUUSD1"], PAPER_BRACKETS)

    def test_slow_cache_read_does_not_extend_ttl_by_network_duration(self):
        now = [100.0]
        calls = []
        def delayed(*args, **kwargs):
            calls.append(args)
            now[0] += 6
            return {"version": len(calls)}
        self.api.call = delayed
        with patch("trading.exchange.time.monotonic", side_effect=lambda: now[0]):
            first = self.broker.cached_call("bracket", "/bracket", ttl=5)
            second = self.broker.cached_call("bracket", "/bracket", ttl=5)
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)

    def test_cache_reuses_fresh_data_but_failed_refresh_never_returns_stale(self):
        self.api.responses["/cache"] = {"value": 1}
        with patch("trading.exchange.time.monotonic", return_value=100):
            first = self.broker.cached_call("test", "/cache", ttl=5)
        with patch("trading.exchange.time.monotonic", return_value=104.999):
            self.assertEqual(self.broker.cached_call("test", "/cache", ttl=5), first)
        self.api.responses["/cache"] = ExchangeError("模拟失败")
        with patch("trading.exchange.time.monotonic", return_value=105):
            with self.assertRaises(ExchangeError):
                self.broker.cached_call("test", "/cache", ttl=5)
        self.api.responses["/cache"] = {"value": 2}
        with patch("trading.exchange.time.monotonic", return_value=105.001):
            self.assertEqual(self.broker.cached_call("test", "/cache", ttl=5), {"value": 2})
        self.assertEqual(len(self.api.calls), 3)


class QuoteHardeningTests(unittest.TestCase):
    def market(self, bid_time=100000, mark_time=100000):
        return MarketData(FixtureAPI({
            "/fapi/v3/premiumIndex": {"symbol": "XAUUSD1", "markPrice": "100", "time": mark_time},
            "/fapi/v3/ticker/bookTicker": {"symbol": "XAUUSD1", "bidPrice": "100", "askPrice": "100.01",
                "bidQty": "10", "askQty": "10", "time": bid_time},
        }))

    def test_both_independent_timestamps_must_be_finite_and_fresh(self):
        for source in ("bid", "mark"):
            for stamp in (104000, 96999, "NaN", "Infinity", None, True, -1):
                with self.subTest(source=source, stamp=stamp), patch("trading.exchange.time.time", return_value=100):
                    market = self.market(**{source + "_time": stamp})
                    with self.assertRaises(TradingError):
                        market.book("XAUUSD1")

    def test_exact_freshness_boundaries_and_oldest_timestamp_are_preserved(self):
        for bid, mark in ((97000, 101000), (101000, 97000), (100000, 100000)):
            with self.subTest(bid=bid, mark=mark), patch("trading.exchange.time.time", return_value=100):
                market = self.market(bid, mark)
                self.assertEqual(market.book("XAUUSD1").timestamp, min(bid, mark) / 1000)
                self.assertEqual([call[1] for call in market.api.calls],
                                 ["/fapi/v3/premiumIndex", "/fapi/v3/ticker/bookTicker"])

    def test_malformed_or_mismatched_quote_sources_fail_closed(self):
        for path in ("/fapi/v3/premiumIndex", "/fapi/v3/ticker/bookTicker"):
            for value in ([], None, {"symbol": "secret-other-symbol"}):
                with self.subTest(path=path, value=value):
                    market = self.market()
                    market.api.responses[path] = value
                    with self.assertRaises(TradingError) as caught:
                        market.book("XAUUSD1")
                    self.assertNotIn("secret", str(caught.exception))


class TransportHardeningTests(unittest.TestCase):
    def setUp(self):
        # Deterministic public test key only; never user credentials or live funds.
        key = bytes(range(1, 33))
        self.credentials = {"private_key": key, "signer": Account.from_key(key).address, "user": "0x" + "22" * 20}

    def api(self, handle):
        api = API(self.credentials, transport=httpx.MockTransport(handle), budget=RateBudget())
        self.addCleanup(api.close)
        return api

    def test_transport_timeout_write_is_ambiguous_and_never_retried(self):
        for method in ("POST", "DELETE"):
            seen = []
            def handle(request):
                seen.append(request)
                raise httpx.ReadTimeout("secret-url-and-signature", request=request)
            with self.subTest(method=method):
                api = self.api(handle)
                with self.assertRaises(AmbiguousOrder) as caught:
                    api.call(method, "/fapi/v3/order", signed=True)
                self.assertEqual(len(seen), 1)
                self.assertNotIn("secret", str(caught.exception))

    def test_http408_and_invalid_json_after_write_are_ambiguous(self):
        for status, body in ((408, "{}"), (503, "{}"), (200, "invalid-secret-response")):
            seen = []
            api = self.api(lambda req: seen.append(req) or httpx.Response(status, text=body))
            with self.subTest(status=status), self.assertRaises(AmbiguousOrder) as caught:
                api.call("POST", "/fapi/v3/order", signed=True)
            self.assertEqual(len(seen), 1)
            self.assertNotIn("secret", str(caught.exception))

    def test_known_exchange_timeouts_are_ambiguous_only_for_writes(self):
        for code in (-1006, -1007):
            for method in ("POST", "GET"):
                api = self.api(lambda req: httpx.Response(400, json={"code": code, "msg": "secret-upstream"}))
                with self.subTest(code=code, method=method), self.assertRaises(ExchangeError) as caught:
                    api.call(method, "/fapi/v3/order", signed=True)
                self.assertEqual(isinstance(caught.exception, AmbiguousOrder), method == "POST")
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn("secret", str(caught.exception))

    def test_redirect_is_not_followed_and_signed_query_never_leaks_to_other_host(self):
        seen = []
        api = self.api(lambda req: seen.append(req) or httpx.Response(307, headers={"Location": "https://example.invalid/steal"}, json={}))
        with self.assertRaises(ExchangeError):
            api.call("GET", "/fapi/v3/order", signed=True)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].url.host, "fapi.asterdex.com")

    def test_invalid_retry_after_cannot_permanently_poison_budget(self):
        for value in ("Infinity", "NaN", "bad-value", "-100"):
            api = self.api(lambda req: httpx.Response(429, headers={"Retry-After": value}))
            with self.subTest(value=value), self.assertRaises(ExchangeError) as caught:
                api.call("GET", "/fapi/v3/time")
            self.assertEqual(caught.exception.retry_after, 180)

    def test_nonce_is_unique_during_parallel_signing_and_backward_wall_clock(self):
        api = self.api(lambda req: httpx.Response(200, json={}))
        with patch("trading.exchange.time.time_ns", return_value=1000000000):
            with ThreadPoolExecutor(max_workers=8) as executor:
                nonces = list(executor.map(lambda _: int(api.signed_parameters({})["nonce"]), range(32)))
        with patch("trading.exchange.time.time_ns", return_value=1):
            final = int(api.signed_parameters({})["nonce"])
        self.assertEqual(sorted(nonces), list(range(1000000, 1000032)))
        self.assertEqual(final, 1000032)


class RateBudgetHardeningTests(unittest.TestCase):
    def test_public_capacity_backoff_cannot_poison_worker_schedule(self):
        for delay in (float("inf"), float("nan"), -1, "invalid"):
            api = Mock()
            api.budget = RateBudget()
            market = MarketData(api)
            with self.subTest(delay=delay), patch("monitor.sample", side_effect=monitor.MonitorError("HTTP 429", delay)):
                with self.assertRaises(ExchangeError) as caught:
                    market.capacities("XAUUSD1", [4, 5, 10, 20])
                self.assertEqual(caught.exception.retry_after, 180)
                with self.assertRaises(ExchangeError) as blocked:
                    api.budget.reserve(1)
                self.assertGreater(blocked.exception.retry_after, 0)
                self.assertLessEqual(blocked.exception.retry_after, 180)

    def test_parallel_reservations_never_overspend(self):
        budget = RateBudget()
        budget.limit = 37
        gate = threading.Barrier(8)
        def reserve_many(_):
            gate.wait()
            accepted = 0
            for _ in range(10):
                try:
                    budget.reserve(1)
                    accepted += 1
                except ExchangeError:
                    pass
            return accepted
        with ThreadPoolExecutor(max_workers=8) as executor:
            accepted = sum(executor.map(reserve_many, range(8)))
        self.assertEqual(accepted, budget.snapshot()["ordinary_limit"])
        self.assertEqual(budget.weight, accepted)

    def test_window_boundary_and_backoff_do_not_reset_each_other(self):
        with patch("trading.exchange.time.monotonic", return_value=100):
            budget = RateBudget()
            budget.limit = 2
            budget.reserve(2)
        with patch("trading.exchange.time.monotonic", return_value=159.999):
            with self.assertRaises(ExchangeError) as caught:
                budget.reserve(1)
            self.assertGreater(caught.exception.retry_after, 0)
        with patch("trading.exchange.time.monotonic", return_value=160):
            budget.reserve(2)
            budget.block(100)
            budget.block(1)
        with patch("trading.exchange.time.monotonic", return_value=220):
            with self.assertRaises(ExchangeError) as caught:
                budget.reserve(1)
            self.assertEqual(caught.exception.retry_after, 40)
        with patch("trading.exchange.time.monotonic", return_value=260):
            budget.reserve(2)
            self.assertEqual(budget.weight, 2)

    def test_invalid_weights_cannot_reduce_or_poison_budget(self):
        budget = RateBudget()
        budget.reserve(2)
        for weight in (-1, 0, True, None, 1.5, float("nan")):
            with self.subTest(weight=weight), self.assertRaises(ExchangeError):
                budget.reserve(weight)
        self.assertEqual(budget.weight, 2)
