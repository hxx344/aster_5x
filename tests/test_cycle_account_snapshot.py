"""Official account fields and conditional reads, with no live exchange access."""
import copy
from dataclasses import replace
from fractions import Fraction
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from trading.exchange import AccountModeError, ExchangeError, LiveBroker, RequestNotSent
from trading.engine import snapshot_json
from trading.models import Book, TradingError, dec
from trading.paper import DemoMarket, PAPER_BRACKETS
from .test_exchange_hardening import FixtureAPI


SYMBOL = "XAUUSD1"
ACCOUNT = "/fapi/v3/accountWithJoinMargin"
MULTI = "/fapi/v3/multiAssetsMargin"
ORDERS = "/fapi/v3/openOrders"
RISK = "/fapi/v3/positionRisk"
BRACKET = "/fapi/v3/leverageBracket"


def account_row(symbol=SYMBOL, side="LONG", *, amount="0", leverage="5", entry="0", pnl="0", cap="1000000"):
    # These are accountWithJoinMargin fields, not positionRisk fields.
    return {"symbol": symbol, "positionSide": side, "positionAmt": amount, "entryPrice": entry,
            "leverage": leverage, "isolated": False, "unrealizedProfit": pnl, "maxNotional": cap,
            "initialMargin": "0", "maintMargin": "0", "positionInitialMargin": "0",
            "openOrderInitialMargin": "0", "updateTime": 0}


def cycle_account_responses(symbols=(SYMBOL,)):
    return {MULTI: {"multiAssetsMargin": False},
            ACCOUNT: {"canTrade": True,
                      "assets": [{"asset": "USD1", "crossWalletBalance": "200", "crossUnPnl": "0",
                                  "maintMargin": "5", "availableBalance": "150", "marginAvailable": True}],
                      "positions": [account_row(symbol, side) for symbol in symbols for side in ("LONG", "SHORT")]}}


def risk_row(row, *, mark="100", pnl=None):
    return {"symbol": row["symbol"], "positionSide": row["positionSide"], "positionAmt": row["positionAmt"],
            "entryPrice": row["entryPrice"], "leverage": row["leverage"], "marginType": "isolated" if row["isolated"] else "cross",
            "unRealizedProfit": row["unrealizedProfit"] if pnl is None else pnl, "markPrice": mark,
            "liquidationPrice": "0"}


class LocalQuoteMarket(DemoMarket):
    demo = False

    def __init__(self):
        super().__init__()
        self.marks = {SYMBOL: dec(100), "SPCXUSD1": dec(200), "CLUSD1": dec(50)}
        self.connected = set(self.marks)
        self.public_reads = []

    def _quote(self, symbol):
        mark = self.marks[symbol]
        return Book(mark - dec(".01"), mark + dec(".01"), dec(100), dec(100), mark, time.time())

    def _stream_book(self, symbol):
        return self._quote(symbol) if symbol in self.connected else None

    def book(self, symbol):
        self.public_reads.append(symbol)
        return self._quote(symbol)


class CycleAccountSnapshotTests(unittest.TestCase):
    def make_broker(self, responses=None, market=None):
        responses = cycle_account_responses() if responses is None else responses
        api = FixtureAPI(responses)
        market = LocalQuoteMarket() if market is None else market
        return LiveBroker({}, market, api=api), api, market

    def test_official_account_only_needs_two_cold_then_one_steady_get(self):
        broker, api, market = self.make_broker()
        first = broker.cycle_snapshot([SYMBOL])
        self.assertCountEqual([call[1] for call in api.calls], [ACCOUNT, MULTI])
        api.calls.clear()
        api.responses[ACCOUNT]["assets"][0]["crossWalletBalance"] = "201"
        second = broker.cycle_snapshot([SYMBOL])
        self.assertCountEqual([call[1] for call in api.calls], [ACCOUNT])
        self.assertEqual(first.equity, dec(200))
        self.assertEqual(second.equity, dec(201))
        self.assertEqual(first.current_leverage_caps, {SYMBOL: (5, dec(1000000))})
        self.assertEqual(first.cycle_cap_cached_at, {})
        self.assertEqual(first.brackets, {})
        self.assertIsNone(first.open_orders)
        self.assertTrue(first.hedge_mode)
        self.assertFalse(first.multi_assets)
        self.assertTrue(all(position.liquidation is None for position in first.positions))
        view = snapshot_json(first, [SYMBOL])
        self.assertNotIn("current_leverage_caps", view)
        self.assertNotIn("cycle_cap_cached_at", view)
        self.assertTrue(all(row["liquidation"] is None for row in view["positions"]))
        self.assertEqual(set(broker.cached_at), {"multi"})
        self.assertEqual(market.public_reads, [])

    def test_account_pnl_uses_exact_official_field_name(self):
        responses = cycle_account_responses()
        responses[ACCOUNT]["positions"][0]["unRealizedProfit"] = "999999"
        broker, _, _ = self.make_broker(responses)
        self.assertEqual(broker.cycle_snapshot([SYMBOL]).unrealized, 0)
        del responses[ACCOUNT]["positions"][0]["unrealizedProfit"]
        with self.assertRaises(TradingError):
            broker.cycle_snapshot([SYMBOL], fresh_modes=True)
        self.assertIsNone(broker.leverage_snapshot)

    def test_all_active_usd1_symbols_use_latest_stream_marks_for_pnl_and_margin(self):
        responses = cycle_account_responses()
        responses[ACCOUNT]["assets"][0]["crossUnPnl"] = "10"
        responses[ACCOUNT]["positions"].extend([
            account_row("SPCXUSD1", "LONG", amount="2", leverage="10", entry="200", pnl="16"),
            account_row("SPCXUSD1", "SHORT", amount="-1", leverage="10", entry="200", pnl="-8")])
        broker, api, market = self.make_broker(responses)
        market.marks["SPCXUSD1"] = dec(220)
        first = broker.cycle_snapshot([SYMBOL])
        self.assertEqual(first.unrealized, dec(8))  # min(asset=10, positions=8, marked=20)
        self.assertEqual(first.equity, dec(208))
        self.assertEqual(first.available, dec(148))
        self.assertEqual(first.occupied_margin_exact, Fraction(66))
        market.marks["SPCXUSD1"] = dec(180)
        api.calls.clear()
        second = broker.cycle_snapshot([SYMBOL])
        self.assertEqual(second.unrealized, dec(-20))
        self.assertEqual(second.equity, dec(180))
        self.assertEqual(second.available, dec(120))
        self.assertEqual(second.occupied_margin_exact, Fraction(54))
        self.assertEqual({p.symbol for p in second.positions if p.qty}, {"SPCXUSD1"})
        self.assertCountEqual([call[1] for call in api.calls], [ACCOUNT])
        self.assertEqual(market.public_reads, [])

    def test_asset_loss_remains_binding_when_position_and_mark_pnl_are_higher(self):
        responses = cycle_account_responses()
        responses[ACCOUNT]["assets"][0]["crossUnPnl"] = "-12"
        responses[ACCOUNT]["positions"][0].update(positionAmt="1", entryPrice="100", unrealizedProfit="-3")
        broker, _, market = self.make_broker(responses)
        market.marks[SYMBOL] = dec(99)
        result = broker.cycle_snapshot([SYMBOL])
        self.assertEqual(result.unrealized, dec(-12))
        self.assertEqual(result.equity, dec(188))
        self.assertEqual(result.available, dec(150))
        self.assertEqual(result.current_leverage_caps, {SYMBOL: (5, dec("1000000"))})

    def test_unsubscribed_position_adds_one_risk_read_without_public_rest(self):
        responses = cycle_account_responses()
        outside = account_row("OTHERUSD1", amount="2", leverage="10", entry="80", pnl="-10")
        responses[ACCOUNT]["positions"].append(outside)
        responses[RISK] = [risk_row(outside, mark="70", pnl="-30")]
        responses[RISK][0]["liquidationPrice"] = "45"
        broker, api, market = self.make_broker(responses)
        market.assets["OTHERUSD1"] = "USD1"
        result = broker.cycle_snapshot([SYMBOL])
        self.assertCountEqual([call[1] for call in api.calls], [ACCOUNT, MULTI, RISK])
        self.assertEqual(result.occupied_margin_exact, Fraction(14))
        self.assertEqual(result.unrealized, dec(-30))
        outside_position = next(p for p in result.positions if p.symbol == "OTHERUSD1")
        self.assertEqual(outside_position.unrealized, dec(-10))
        self.assertEqual(outside_position.liquidation, dec(45))
        self.assertEqual(market.public_reads, [])

    def test_missing_stream_uses_risk_marks_for_flat_symbol(self):
        responses = cycle_account_responses()
        responses[RISK] = [risk_row(row, mark="110") for row in responses[ACCOUNT]["positions"]]
        broker, api, market = self.make_broker(responses)
        market.connected.clear()
        result = broker.cycle_snapshot([SYMBOL])
        self.assertTrue(all(p.mark == 110 for p in result.positions))
        self.assertEqual([call[1] for call in api.calls].count(RISK), 1)
        self.assertEqual(market.public_reads, [])

    def test_only_flat_risk_omission_uses_one_existing_public_quote_fallback(self):
        responses = cycle_account_responses()
        responses[RISK] = []
        broker, api, market = self.make_broker(responses)
        market.connected.clear()
        result = broker.cycle_snapshot([SYMBOL])
        self.assertEqual(market.public_reads, [SYMBOL])
        self.assertTrue(all(p.mark == 100 and p.liquidation is None for p in result.positions))
        self.assertEqual([call[1] for call in api.calls].count(RISK), 1)

    def test_stale_local_book_uses_risk_instead_of_refreshing_public_display_fields(self):
        responses = cycle_account_responses()
        responses[RISK] = [risk_row(row) for row in responses[ACCOUNT]["positions"]]
        broker, api, market = self.make_broker(responses)
        original = market._stream_book
        market._stream_book = lambda symbol: replace(original(symbol), timestamp=time.time() - 4)
        broker.cycle_snapshot([SYMBOL])
        self.assertEqual([call[1] for call in api.calls].count(RISK), 1)
        self.assertEqual(market.public_reads, [])

    def test_risk_fallback_rejects_whole_account_inconsistency_without_authorization(self):
        def mutate_quantity(rows):
            rows[0]["positionAmt"] = "1.00000000000000000000000000001"
        def mutate_leverage(rows):
            rows[0]["leverage"] = "6"
        def mutate_margin(rows):
            rows[0]["marginType"] = "isolated"
        def add_external(rows):
            rows.append({**rows[0], "symbol": "CLUSD1"})
        def omit_active(rows):
            rows.pop(0)
        def switch_mode(rows):
            rows.append({**rows[1], "positionSide": "BOTH"})
        for mutate in (mutate_quantity, mutate_leverage, mutate_margin, add_external, omit_active, switch_mode):
            with self.subTest(mutate=mutate.__name__):
                responses = cycle_account_responses()
                responses[ACCOUNT]["positions"][0].update(positionAmt="1", entryPrice="100")
                responses[RISK] = [risk_row(row) for row in responses[ACCOUNT]["positions"]]
                mutate(responses[RISK])
                broker, api, market = self.make_broker(responses)
                market.connected.clear()
                with self.assertRaises(TradingError):
                    broker.cycle_snapshot([SYMBOL], fresh_modes=True)
                self.assertEqual([call[1] for call in api.calls].count(RISK), 1)
                self.assertEqual(broker.cached_at, {})
                self.assertIsNone(broker.leverage_snapshot)
                self.assertEqual(market.public_reads, [])

    def test_current_caps_keep_zero_and_choose_lower_leg_at_actual_leverage(self):
        for first, second, expected in (("0", "100", "0"), ("20", "10", "10"), ("2.5", "3", "2.5")):
            with self.subTest(first=first, second=second):
                responses = cycle_account_responses()
                for row, cap in zip(responses[ACCOUNT]["positions"], (first, second)):
                    row.update(maxNotional=cap, leverage="7")
                broker, api, _ = self.make_broker(responses)
                snapshot = broker.cycle_snapshot([SYMBOL])
                self.assertEqual(snapshot.current_leverage_caps, {SYMBOL: (7, dec(expected))})
                self.assertEqual(snapshot.brackets, {})
                self.assertEqual(len(api.calls), 2)

    def test_missing_cap_uses_one_validated_tier_read_without_manufacturing_tiers(self):
        responses = cycle_account_responses()
        del responses[ACCOUNT]["positions"][0]["maxNotional"]
        responses[ACCOUNT]["positions"][1]["maxNotional"] = "500"
        responses[BRACKET] = {"symbol": SYMBOL, "brackets": copy.deepcopy(PAPER_BRACKETS)}
        broker, api, _ = self.make_broker(responses)
        first = broker.cycle_snapshot([SYMBOL])
        self.assertEqual(first.current_leverage_caps, {SYMBOL: (5, dec(500))})
        self.assertEqual(first.brackets, {})
        self.assertEqual([call[1] for call in api.calls].count(BRACKET), 1)
        api.calls.clear()
        broker.cycle_snapshot([SYMBOL])
        self.assertCountEqual([call[1] for call in api.calls], [ACCOUNT])

    def test_holding_does_not_need_a_cap_or_tier_table(self):
        responses = cycle_account_responses()
        for row in responses[ACCOUNT]["positions"]:
            row.update(positionAmt="1", entryPrice="100")
            del row["maxNotional"]
        responses[BRACKET] = ExchangeError("capacity unavailable")
        broker, api, _ = self.make_broker(responses)
        snapshot = broker.cycle_snapshot([SYMBOL])
        self.assertEqual(snapshot.current_leverage_caps, {})
        self.assertEqual(snapshot.brackets, {})
        self.assertEqual(len(api.calls), 3)

    def test_cached_fallback_cap_keeps_original_ttl_until_final_submit_check(self):
        clock = SimpleNamespace(now=100.0)
        responses = cycle_account_responses()
        for row in responses[ACCOUNT]["positions"]:
            del row["maxNotional"]
        responses[BRACKET] = {"symbol": SYMBOL, "brackets": copy.deepcopy(PAPER_BRACKETS)}
        broker, api, _ = self.make_broker(responses)
        with patch("trading.exchange.time.monotonic", side_effect=lambda: clock.now):
            initial = broker.cycle_snapshot([SYMBOL])
            self.assertEqual(initial.cycle_cap_cached_at, {SYMBOL: 100})
            api.calls.clear()
            clock.now = 104.9
            snapshot = broker.cycle_snapshot([SYMBOL])
            self.assertEqual(snapshot.cycle_cap_cached_at, {SYMBOL: 100})
            self.assertEqual(snapshot.current_leverage_caps, {SYMBOL: (5, dec(1000000))})
            snapshot.require_fresh()
            self.assertCountEqual([call[1] for call in api.calls], [ACCOUNT])
            api.calls.clear()
            clock.now = 105.1
            with self.assertRaises(TradingError):
                snapshot.require_fresh()
            self.assertEqual(api.calls, [])

    def test_malformed_reported_cap_does_not_fall_back_to_a_larger_limit(self):
        for value in (None, True, "-1", "NaN", "Infinity", "bad"):
            with self.subTest(value=value):
                responses = cycle_account_responses()
                responses[ACCOUNT]["positions"][0]["maxNotional"] = value
                broker, api, _ = self.make_broker(responses)
                with self.assertRaises(TradingError):
                    broker.cycle_snapshot([SYMBOL], fresh_modes=True)
                self.assertEqual(len(api.calls), 2)
                self.assertIsNone(broker.leverage_snapshot)
                self.assertEqual(broker.cached_at, {})

    def test_missing_cap_rejects_wrong_or_malformed_tiers(self):
        for data in ({"symbol": "CLUSD1", "brackets": PAPER_BRACKETS}, [],
                     {"symbol": SYMBOL, "brackets": ["bad"]},
                     {"symbol": SYMBOL, "brackets": [{}]},
                     {"symbol": SYMBOL, "brackets": [{**PAPER_BRACKETS[0], "notionalCap": "-1"}]}):
            with self.subTest(data=data):
                responses = cycle_account_responses()
                del responses[ACCOUNT]["positions"][0]["maxNotional"]
                responses[BRACKET] = data
                broker, _, _ = self.make_broker(responses)
                with self.assertRaises(TradingError):
                    broker.cycle_snapshot([SYMBOL], fresh_modes=True)
                self.assertEqual(broker.cached_at, {})
                self.assertIsNone(broker.leverage_snapshot)

    def test_bad_account_identity_or_exposure_is_rejected(self):
        def both(account):
            account["positions"][0]["positionSide"] = "BOTH"
        def duplicate(account):
            account["positions"].append(copy.deepcopy(account["positions"][0]))
        def missing(account):
            account["positions"].pop()
        def leverage(account):
            account["positions"][0]["leverage"] = "10"
        def foreign(account):
            account["positions"].append(account_row("BTCUSDT", amount="1", entry="100"))
        def no_asset(account):
            account["assets"].clear()
        def repeat_asset(account):
            account["assets"] *= 2
        def invalid_isolated(account):
            account["positions"][0]["isolated"] = "false"
        for mutate in (both, duplicate, missing, leverage, foreign, no_asset, repeat_asset, invalid_isolated):
            with self.subTest(mutate=mutate.__name__):
                responses = cycle_account_responses()
                mutate(responses[ACCOUNT])
                broker, api, _ = self.make_broker(responses)
                with self.assertRaises(TradingError):
                    broker.cycle_snapshot([SYMBOL], fresh_modes=True)
                self.assertIsNone(broker.leverage_snapshot)
                self.assertEqual(broker.cached_at, {})
                self.assertTrue(all(call[0] == "GET" for call in api.calls))

    def test_multi_cache_and_force_refresh_never_publish_or_invalidate_fake_dual_data(self):
        clock = SimpleNamespace(now=100.0)
        broker, api, _ = self.make_broker()
        broker.cached["dual"], broker.cached_at["dual"] = {"dualSidePosition": False}, 88
        with patch("trading.exchange.time.monotonic", side_effect=lambda: clock.now):
            broker.cycle_snapshot([SYMBOL], fresh_modes=True)
            self.assertEqual(broker.cached_at, {"dual": 88, "multi": 100})
            clock.now = 114.9
            api.responses[MULTI]["multiAssetsMargin"] = True
            self.assertFalse(broker.cycle_snapshot([SYMBOL]).multi_assets)
            clock.now = 115
            self.assertTrue(broker.cycle_snapshot([SYMBOL]).multi_assets)
            self.assertEqual([call[1] for call in api.calls].count(MULTI), 2)
            self.assertEqual(broker.cached_at["multi"], 115)
            api.responses[MULTI]["multiAssetsMargin"] = False
            self.assertFalse(broker.cycle_snapshot([SYMBOL], fresh_modes=True).multi_assets)
            self.assertEqual([call[1] for call in api.calls].count(MULTI), 3)
        self.assertEqual(broker.cached["dual"], {"dualSidePosition": False})
        self.assertEqual(broker.cached_at["dual"], 88)

    def test_new_account_mode_is_detected_even_with_valid_multi_cache(self):
        broker, api, _ = self.make_broker()
        broker.cycle_snapshot([SYMBOL])
        api.calls.clear()
        api.responses[ACCOUNT]["positions"] = [account_row(side="BOTH")]
        with self.assertRaises(AccountModeError):
            broker.cycle_snapshot([SYMBOL])
        self.assertCountEqual([call[1] for call in api.calls], [ACCOUNT])

    def test_unknown_account_read_cannot_send_a_leverage_order(self):
        responses = cycle_account_responses()
        responses[ACCOUNT] = RequestNotSent("unknown private read")
        broker, api, _ = self.make_broker(responses)
        with self.assertRaises(RequestNotSent):
            broker.set_cycle_leverage(SYMBOL, 6)
        self.assertIsNone(broker.leverage_snapshot)
        self.assertEqual(broker.cached_at, {})
        self.assertEqual(len(api.calls), 2)
        self.assertTrue(all(call[0] == "GET" for call in api.calls))

    def test_weight_estimate_covers_conditional_reads_without_issuing_requests(self):
        clock = SimpleNamespace(now=100.0)
        broker, api, _ = self.make_broker()
        with patch("trading.exchange.time.monotonic", side_effect=lambda: clock.now):
            self.assertEqual(broker.cycle_snapshot_weight([SYMBOL]), 42)
            broker.cycle_snapshot([SYMBOL])
            api.calls.clear()
            self.assertEqual(broker.cycle_snapshot_weight([SYMBOL]), 12)
            self.assertEqual(broker.cycle_snapshot_weight([SYMBOL], fresh_modes=True), 42)
            clock.now = 107
            self.assertEqual(broker.cycle_snapshot_weight([SYMBOL]), 42)
            self.assertEqual(api.calls, [])


if __name__ == "__main__":
    unittest.main()
