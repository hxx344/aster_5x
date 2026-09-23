"""Holdings outside the strategy use exchange metadata, never symbol suffixes."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import threading
import unittest
from unittest.mock import patch

from tests.test_cycle_account_snapshot import account_row, risk_row, ACCOUNT, RISK, SYMBOL
from tests.test_exchange_hardening import FixtureAPI, account_responses
from tests.test_market_order_rules import exchange_info
from trading.exchange import ExchangeError, LiveBroker, MarketData, RateBudget
from trading.models import TradingError, dec


INFO = "/fapi/v3/exchangeInfo"
OTHER = "MOONSHOTUSD1"


def outside_responses():
    responses = account_responses()
    row = account_row(OTHER, amount="2", leverage="5", entry="90", pnl="20")
    responses[ACCOUNT]["positions"].append(row)
    responses[RISK].append(risk_row(row))
    responses[INFO] = exchange_info()
    return responses


def market_and_api():
    api = FixtureAPI(outside_responses())
    api.budget = RateBudget()
    api.close = lambda: None
    market = MarketData(api)
    market.load_rules()
    api.calls.clear()
    return market, api


def list_other(api, asset="USD1"):
    api.responses[INFO]["symbols"].append({"symbol": OTHER, "marginAsset": asset})


class AccountMarketAssetTests(unittest.TestCase):
    def test_new_usd1_holding_refreshes_once_and_contributes_to_both_snapshot_paths(self):
        for method in ("snapshot", "cycle_snapshot"):
            with self.subTest(method=method):
                market, api = market_and_api()
                list_other(api)
                rules = deepcopy(market.rules)
                broker = LiveBroker({}, market, api=api)
                snapshot = getattr(broker, method)([SYMBOL])
                holding = next(p for p in snapshot.positions if p.symbol == OTHER)
                self.assertEqual((holding.qty, holding.mark, holding.unrealized), (dec(2), dec(100), dec(20)))
                self.assertEqual(snapshot.total_notional, dec(400))
                self.assertEqual(snapshot.occupied_margin, dec(90))
                self.assertEqual(snapshot.unrealized, dec(0))  # Do not fund trades with an unconfirmed gain.
                getattr(broker, method)([SYMBOL])
                reads = [call for call in api.calls if call[1] == INFO]
                self.assertEqual(len(reads), 1)
                self.assertFalse(reads[0][3].get("signed", False))
                self.assertTrue(all(call[0] == "GET" for call in api.calls))
                self.assertEqual(market.rules, rules)
                self.assertNotIn(OTHER, market.rules)
                private_symbols = [call[2][0].get("symbol") for call in api.calls
                                   if call[2] and isinstance(call[2][0], dict)]
                self.assertNotIn(OTHER, private_symbols)

    def test_known_usd1_does_not_refresh_and_losses_remain_in_total_risk(self):
        for method in ("snapshot", "cycle_snapshot"):
            with self.subTest(method=method):
                market, api = market_and_api()
                market.assets[OTHER] = "USD1"
                api.responses[ACCOUNT]["positions"][-1].update(entryPrice="110", unrealizedProfit="-20")
                api.responses[RISK][-1].update(entryPrice="110", unRealizedProfit="-20")
                snapshot = getattr(LiveBroker({}, market, api=api), method)([SYMBOL])
                self.assertEqual((snapshot.equity, snapshot.available, snapshot.unrealized), (dec(180), dec(130), dec(-20)))
                self.assertFalse(any(call[1] == INFO for call in api.calls))

    def test_confirmed_non_usd1_is_blocked_even_with_a_usd1_suffix(self):
        for method in ("snapshot", "cycle_snapshot"):
            for cached in (False, True):
                with self.subTest(method=method, cached=cached):
                    market, api = market_and_api()
                    list_other(api, "USDT")
                    if cached:
                        market.assets[OTHER] = "USDT"
                    with self.assertRaisesRegex(TradingError, "MOONSHOTUSD1.*USDT.*非 USD1"):
                        getattr(LiveBroker({}, market, api=api), method)([SYMBOL])
                    self.assertEqual(sum(call[1] == INFO for call in api.calls), 0 if cached else 1)

    def test_unknown_is_not_misreported_as_non_usd1_and_retries_after_cooldown(self):
        for method in ("snapshot", "cycle_snapshot"):
            with self.subTest(method=method):
                market, api = market_and_api()
                broker = LiveBroker({}, market, api=api)
                for _ in range(2):
                    with self.assertRaisesRegex(TradingError, "MOONSHOTUSD1.*尚未确认") as caught:
                        getattr(broker, method)([SYMBOL])
                    self.assertNotIn("非 USD1", str(caught.exception))
                self.assertEqual(sum(call[1] == INFO for call in api.calls), 1)
                list_other(api)
                market.assets_retry_at = 0
                snapshot = getattr(broker, method)([SYMBOL])
                self.assertIn(OTHER, [p.symbol for p in snapshot.positions])

    def test_invalid_metadata_keeps_last_complete_mapping_and_does_not_loop(self):
        invalid = [None, {}, {"symbols": []}, {"symbols": [None]},
                   {"symbols": [{"symbol": OTHER}]}, {"symbols": [{"symbol": OTHER, "marginAsset": ""}]},
                   {"symbols": [{"symbol": OTHER, "marginAsset": "USD1"},
                                {"symbol": OTHER, "marginAsset": "USDT"}]}]
        for response in invalid:
            with self.subTest(response=response):
                market, api = market_and_api()
                previous = market.assets
                api.responses[INFO] = response
                with self.assertRaises(TradingError):
                    market.margin_asset(OTHER)
                self.assertIs(market.assets, previous)
                self.assertIsNone(market.margin_asset(OTHER))
                self.assertEqual(market.margin_asset(SYMBOL), "USD1")
                self.assertEqual(sum(call[1] == INFO for call in api.calls), 1)

    def test_transport_failure_preserves_error_backoff_and_known_assets(self):
        market, api = market_and_api()
        failure = ExchangeError("metadata cooling down", retry_after=180)
        api.responses[INFO] = failure
        with patch("trading.exchange.time.monotonic", return_value=100):
            with self.assertRaises(ExchangeError) as caught:
                market.margin_asset(OTHER)
            self.assertIs(caught.exception, failure)
            self.assertEqual(market.assets_retry_at, 280)
            self.assertIsNone(market.margin_asset(OTHER))
            self.assertEqual(market.margin_asset(SYMBOL), "USD1")
        self.assertEqual(sum(call[1] == INFO for call in api.calls), 1)

    def test_accounts_share_one_refresh_and_known_reads_do_not_wait_for_it(self):
        for listed in (False, True):
            with self.subTest(listed=listed):
                market, api = market_and_api()
                if listed:
                    list_other(api)
                entered, release = threading.Event(), threading.Event()
                original = api.call

                def read(*args, **kwargs):
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("metadata refresh was not released")
                    return original(*args, **kwargs)

                with patch.object(api, "call", side_effect=read), ThreadPoolExecutor(max_workers=6) as pool:
                    first = pool.submit(market.margin_asset, OTHER)
                    try:
                        self.assertTrue(entered.wait(1))
                        rest = [pool.submit(market.margin_asset, OTHER) for _ in range(4)]
                        self.assertEqual(pool.submit(market.margin_asset, SYMBOL).result(timeout=1), "USD1")
                    finally:
                        release.set()
                    self.assertEqual([future.result(timeout=2) for future in [first, *rest]],
                                     ["USD1" if listed else None] * 5)
                self.assertEqual(sum(call[1] == INFO for call in api.calls), 1)
