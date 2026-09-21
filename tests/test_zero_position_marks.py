"""Zero positionRisk marks must be replaced with fresh market marks, never zero exposure."""
import copy
from dataclasses import replace
import time
import unittest
from unittest.mock import Mock

from trading.engine import Engine
from trading.exchange import LiveBroker, RateBudget, SnapshotSuperseded
from trading.models import TradingError, dec
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
        api = FixtureAPI(responses)
        return LiveBroker({}, market, api=api), api, market

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
                    market.book.assert_called_once_with(SYMBOL)
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
        market.book.assert_called_once_with(SYMBOL)
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
                quote = market._quote(SYMBOL)
                if failure == "unavailable":
                    market.book.side_effect = TradingError("行情未就绪")
                else:
                    market.book.side_effect = None
                    market.book.return_value = replace(quote, **{
                        "stale": {"timestamp": time.time() - 30},
                        "future": {"timestamp": time.time() + 30},
                        "zero": {"mark": dec(0)},
                    }[failure])
                with self.assertRaises(TradingError):
                    broker.snapshot([SYMBOL], fresh_modes=True)
                self.assertIsNone(broker.leverage_snapshot)

    def test_other_invalid_marks_remain_errors_and_valid_marks_do_not_fetch(self):
        for value in ("-1", "NaN", None, "bad"):
            with self.subTest(value=value):
                responses = ordinary_responses()
                responses[RISK][0]["markPrice"] = value
                broker, _, market = self.broker(responses)
                with self.assertRaises(TradingError):
                    broker.snapshot([SYMBOL])
                market.book.assert_not_called()
        broker, _, market = self.broker(ordinary_responses())
        self.assertEqual(broker.snapshot([SYMBOL]).occupied_margin, 50)
        market.book.assert_not_called()

    def test_account_change_during_quote_read_revokes_snapshot(self):
        responses = ordinary_responses()
        responses[RISK][0]["markPrice"] = "0E-8"
        broker, _, market = self.broker(responses)
        def changed(symbol):
            with broker._snapshot_lock:
                broker._snapshot_generation += 1
            return market._quote(symbol)
        market.book.side_effect = changed
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
                market.book.assert_called_once_with(SYMBOL)
                self.assertTrue(all(call[0] == "GET" for call in api.calls))

    def test_zero_mark_does_not_hide_account_position_mismatch(self):
        responses = ordinary_responses()
        responses[RISK][0].update(markPrice="0E-8", positionAmt="2")
        broker, _, market = self.broker(responses)
        with self.assertRaisesRegex(TradingError, "余额与持仓快照正在同步"):
            broker.snapshot([SYMBOL], fresh_modes=True)
        market.book.assert_not_called()
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
