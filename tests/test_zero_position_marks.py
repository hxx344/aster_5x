"""Zero positionRisk marks must be replaced with fresh market marks, never zero exposure."""
import copy
from dataclasses import replace
import time
import unittest
from unittest.mock import Mock, patch

from trading.engine import Engine
from trading.exchange import BudgetWait, ExchangeError, LiveBroker, MarketData, RateBudget, RequestNotSent, SnapshotSuperseded
from trading.models import MarkPrice, Position, TradingError, dec
from trading.paper import PAPER_BRACKETS
from .helpers import Fixture
from .test_cycle_account_snapshot import ACCOUNT, RISK, LocalQuoteMarket, cycle_account_responses, risk_row
from .test_exchange_hardening import FixtureAPI, account_responses


SYMBOL = "CLUSD1"


def ordinary_responses(*, flat=False):
    responses = copy.deepcopy(account_responses())
    for rows in (responses[RISK], responses[ACCOUNT]["positions"]):
        for row in rows:
            row["symbol"] = SYMBOL
            row["positionAmt"] = "0" if flat else "1" if row["positionSide"] == "LONG" else "-1"
            row["entryPrice"] = "0" if flat else "100"
    responses["/fapi/v3/leverageBracket"]["symbol"] = SYMBOL
    return responses


class ZeroPositionMarkTests(unittest.TestCase):
    def broker(self, responses):
        market = LocalQuoteMarket()
        market.book = Mock(wraps=market.book)
        market.mark_price = Mock(side_effect=lambda symbol: MarkPrice(symbol, market.marks[symbol], time.time()))
        market._stream_mark_price = Mock(side_effect=lambda symbol: (
            MarkPrice(symbol, market.marks[symbol], time.time()) if symbol in market.connected else None))
        api = FixtureAPI(responses)
        return LiveBroker({}, market, api=api), api, market

    def test_account_mark_recovery_ignores_stale_bbo_but_trading_still_rejects_it(self):
        for method in ("snapshot", "cycle_snapshot"):
            with self.subTest(method=method), patch("trading.exchange.time.time", return_value=100):
                responses = ordinary_responses()
                responses[RISK][0]["markPrice"] = "0E-8"
                responses["/fapi/v3/premiumIndex"] = {"symbol": SYMBOL, "markPrice": "50", "time": 100000}
                responses["/fapi/v3/ticker/bookTicker"] = {
                    "symbol": SYMBOL, "bidPrice": "49", "askPrice": "51", "bidQty": "10", "askQty": "10", "time": 90000}
                api = FixtureAPI(responses)
                market = MarketData(api)
                market.assets = LocalQuoteMarket().assets
                broker = LiveBroker({}, market, api=api)
                snapshot = getattr(broker, method)([SYMBOL])
                self.assertEqual([position.mark for position in snapshot.positions], [50, 50])
                self.assertEqual(sum(call[1] == "/fapi/v3/premiumIndex" for call in api.calls), 1)
                self.assertFalse(any(call[1] in ("/fapi/v3/ticker/bookTicker", "/fapi/v3/commissionRate") for call in api.calls))
                with self.assertRaises(TradingError):
                    market.book(SYMBOL)
                with self.assertRaises(TradingError):
                    market.cycle_book(SYMBOL)
                self.assertTrue(all(call[0] == "GET" for call in api.calls))

    def test_flat_and_held_zero_marks_use_one_fresh_price_for_both_sides(self):
        for flat in (False, True):
            for zero_sides in (1, 2):
                with self.subTest(flat=flat, zero_sides=zero_sides):
                    responses = ordinary_responses(flat=flat)
                    for row in responses[RISK][:zero_sides]:
                        row["markPrice"] = "0E-8"
                    original = copy.deepcopy(responses)
                    broker, api, market = self.broker(responses)
                    snapshot = broker.snapshot([SYMBOL])
                    self.assertEqual([p.mark for p in snapshot.positions], [50, 50])
                    self.assertEqual(snapshot.total_notional, 0 if flat else 100)
                    self.assertEqual(snapshot.occupied_margin, 0 if flat else 25)
                    self.assertEqual(snapshot.unrealized, 0)
                    market.mark_price.assert_called_once_with(SYMBOL)
                    market.book.assert_not_called()
                    self.assertEqual(responses, original)
                    self.assertTrue(all(call[0] == "GET" for call in api.calls))

    def test_active_external_symbol_and_reported_losses_remain_in_risk(self):
        responses = ordinary_responses()
        for rows in (responses[RISK], responses[ACCOUNT]["positions"]):
            rows[1]["positionAmt"] = "0"
        responses[RISK][0].update(markPrice="0E-8", unRealizedProfit="-80")
        broker, _, market = self.broker(responses)
        # CL is outside the selected strategy, but its existing exposure counts.
        snapshot = broker.snapshot([])
        self.assertEqual(len(snapshot.positions), 1)
        self.assertEqual(snapshot.positions[0].symbol, SYMBOL)
        self.assertEqual(snapshot.occupied_margin, dec("12.5"))
        self.assertEqual(snapshot.unrealized, -80)
        self.assertEqual(snapshot.equity, 120)
        self.assertEqual(snapshot.available, 70)
        market.mark_price.assert_called_once_with(SYMBOL)
        market.book.assert_not_called()
        responses[RISK][0]["unRealizedProfit"] = "0"
        snapshot = broker.snapshot([])
        self.assertEqual(snapshot.unrealized, -50)
        self.assertEqual(snapshot.equity, 150)

    def test_unavailable_stale_future_or_zero_quote_does_not_authorize_snapshot(self):
        for failure in ("unavailable", "stale", "future", "zero"):
            with self.subTest(failure=failure):
                responses = ordinary_responses()
                responses[RISK][0]["markPrice"] = "0E-8"
                broker, _, market = self.broker(responses)
                quote = MarkPrice(SYMBOL, dec(50), time.time())
                if failure == "unavailable":
                    market.mark_price.side_effect = TradingError("行情未就绪")
                else:
                    market.mark_price.side_effect = None
                    market.mark_price.return_value = replace(quote, **{
                        "stale": {"timestamp": time.time() - 30},
                        "future": {"timestamp": time.time() + 30},
                        "zero": {"price": dec(0)},
                    }[failure])
                with self.assertRaises(TradingError):
                    broker.snapshot([SYMBOL], fresh_modes=True)
                self.assertIsNone(broker.leverage_snapshot)

    def test_quote_read_failure_explains_snapshot_context_and_preserves_retry_semantics(self):
        for kind in (TradingError, ExchangeError, RequestNotSent, BudgetWait, SnapshotSuperseded):
            with self.subTest(kind=kind):
                responses = ordinary_responses()
                responses[RISK][0]["markPrice"] = "0E-8"
                broker, _, market = self.broker(responses)
                failure = kind("备用行情失败")
                if isinstance(failure, ExchangeError):
                    failure.code, failure.http_status, failure.retry_after = -1003, 429, 180
                market.mark_price.side_effect = failure
                with self.assertRaises(kind) as caught:
                    broker.snapshot([SYMBOL])
                self.assertIs(caught.exception, failure)
                self.assertIn(f"{SYMBOL} 持仓标记价缺失", str(caught.exception))
                self.assertIn("本次账户快照未更新：备用行情失败", str(caught.exception))
                if isinstance(failure, ExchangeError):
                    self.assertEqual((failure.code, failure.http_status, failure.retry_after), (-1003, 429, 180))
                self.assertIsNone(broker.leverage_snapshot)
                market.mark_price.assert_called_once_with(SYMBOL)
                market.book.assert_not_called()

    def test_other_invalid_marks_remain_errors_and_valid_marks_do_not_fetch(self):
        for value in ("-1", "NaN", None, "bad"):
            with self.subTest(value=value):
                responses = ordinary_responses()
                responses[RISK][0]["markPrice"] = value
                broker, _, market = self.broker(responses)
                with self.assertRaises(TradingError):
                    broker.snapshot([SYMBOL])
                market.book.assert_not_called()
                market.mark_price.assert_not_called()
        broker, _, market = self.broker(ordinary_responses())
        self.assertEqual(broker.snapshot([SYMBOL]).occupied_margin, 50)
        market.book.assert_not_called()
        market.mark_price.assert_not_called()

    def test_account_change_during_quote_read_revokes_snapshot(self):
        responses = ordinary_responses()
        responses[RISK][0]["markPrice"] = "0E-8"
        broker, _, market = self.broker(responses)
        def changed(symbol):
            with broker._snapshot_lock:
                broker._snapshot_generation += 1
            return MarkPrice(symbol, dec(50), time.time())
        market.mark_price.side_effect = changed
        with self.assertRaises(SnapshotSuperseded):
            broker.snapshot([SYMBOL], fresh_modes=True)
        self.assertIsNone(broker.leverage_snapshot)

    def test_cycle_background_read_recovers_zero_marks_with_consistent_risk(self):
        for flat in (False, True):
            with self.subTest(flat=flat):
                responses = cycle_account_responses((SYMBOL,))
                if not flat:
                    for row in responses[ACCOUNT]["positions"]:
                        row.update(positionAmt="1" if row["positionSide"] == "LONG" else "-1", entryPrice="100")
                responses[RISK] = [risk_row(row) for row in responses[ACCOUNT]["positions"]]
                responses[RISK][1]["markPrice"] = "0E-8"
                broker, api, market = self.broker(responses)
                market.connected.clear()
                snapshot = broker.cycle_snapshot([SYMBOL])
                self.assertEqual([p.mark for p in snapshot.positions], [50, 50])
                self.assertEqual(snapshot.occupied_margin, 0 if flat else 20)
                self.assertEqual(snapshot.unrealized, 0)
                market.mark_price.assert_called_once_with(SYMBOL)
                market.book.assert_not_called()
                self.assertTrue(all(call[0] == "GET" for call in api.calls))

    def test_zero_mark_does_not_hide_account_position_mismatch(self):
        responses = ordinary_responses()
        responses[RISK][0].update(markPrice="0E-8", positionAmt="2")
        broker, _, market = self.broker(responses)
        with self.assertRaisesRegex(TradingError, "余额与持仓快照正在同步"):
            broker.snapshot([SYMBOL], fresh_modes=True)
        market.book.assert_not_called()
        market.mark_price.assert_not_called()
        self.assertIsNone(broker.leverage_snapshot)

    def test_cycle_local_mark_needs_no_bbo_and_never_starts_rest(self):
        broker, _, market = self.broker(ordinary_responses())
        market._stream_book = Mock(side_effect=AssertionError("BBO should not be read"))
        self.assertEqual(broker._cycle_local_mark(SYMBOL), dec(50))
        market.connected.clear()
        self.assertIsNone(broker._cycle_local_mark(SYMBOL))
        market.mark_price.assert_not_called()
        market.book.assert_not_called()

    def test_earlier_mark_is_revalidated_after_another_symbol_read(self):
        broker, _, market = self.broker(ordinary_responses())
        now = [100.0]
        positions = [Position(symbol, "LONG", dec(1), dec(100), dec(0), 5)
                     for symbol in (SYMBOL, "XAUUSD1")]

        def delayed(symbol):
            if symbol == "XAUUSD1":
                now[0] += 4
            return MarkPrice(symbol, dec(50), now[0])

        market.mark_price.side_effect = delayed
        with patch("trading.models.time.time", side_effect=lambda: now[0]), self.assertRaises(TradingError):
            broker._fill_missing_marks(positions)
        self.assertEqual([position.mark for position in positions], [0, 0])

    def test_wrong_symbol_mark_cannot_revalue_snapshot(self):
        responses = ordinary_responses()
        responses[RISK][0]["markPrice"] = "0"
        broker, _, market = self.broker(responses)
        market.mark_price.side_effect = None
        market.mark_price.return_value = MarkPrice("XAUUSD1", dec(50), time.time())
        with self.assertRaises(TradingError):
            broker.snapshot([SYMBOL])
        self.assertIsNone(broker.leverage_snapshot)

    def test_new_account_loads_all_positions_and_stays_paused(self):
        fixture = Fixture()
        self.addCleanup(fixture.close)
        responses = ordinary_responses()
        for symbol in ("XAUUSD1", "SPCXUSD1"):
            extra = account_responses()
            for row in extra[RISK] + extra[ACCOUNT]["positions"]:
                row.update(symbol=symbol, positionAmt="0", entryPrice="0")
            responses[RISK].extend(extra[RISK])
            responses[ACCOUNT]["positions"].extend(extra[ACCOUNT]["positions"])
        responses[RISK][0]["markPrice"] = "0E-8"
        broker, api, market = self.broker(responses)
        original_call = api.call
        def call(method, path, params=None, **kwargs):
            result = original_call(method, path, params, **kwargs)
            if path == "/fapi/v3/leverageBracket":
                return {"symbol": params["symbol"], "brackets": copy.deepcopy(PAPER_BRACKETS)}
            return result
        api.call = call
        api.budget = RateBudget()
        engine = Engine(fixture.store, market=market)
        engine.ready = True
        engine.add_account({"id": "new", "name": "New", "env_prefix": "ASTER_NEW", "mode": "live"})
        engine.brokers["new"] = broker
        engine.tick_account("new")
        new = next(row for row in engine.state()["accounts"] if row["id"] == "new")
        self.assertEqual(new["status"], "paused")
        self.assertTrue(new["credential_ready"])
        self.assertFalse(new["enabled"])
        self.assertEqual(len(new["snapshot"]["positions"]), 6)
        self.assertEqual(dec(new["snapshot"]["occupied_margin"]), 25)
        self.assertIsNone(fixture.store.intent("new"))
        self.assertTrue(all(call[0] == "GET" for call in api.calls))


if __name__ == "__main__":
    unittest.main()
