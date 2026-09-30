"""Collateral reads retain risk evidence without requiring opening capacity."""
from fractions import Fraction
from unittest import TestCase
from unittest.mock import patch

from tests.test_cycle_account_snapshot import (
    ACCOUNT, BRACKET, MULTI, RISK, SYMBOL, LocalQuoteMarket,
    account_row, cycle_account_responses, risk_row,
)
from tests.test_exchange_hardening import FixtureAPI
from trading.exchange import LiveBroker
from trading.models import AccountModeError, TradingError, dec
from trading.paper import PAPER_BRACKETS


DUAL = "/fapi/v3/positionSide/dual"


class MarginAccountSnapshotTests(TestCase):
    def broker(self, extra=None):
        responses = cycle_account_responses()
        if extra:
            responses[ACCOUNT]["positions"].append(extra)
        responses[DUAL] = {"dualSidePosition": True}
        responses[RISK] = [risk_row(row) for row in responses[ACCOUNT]["positions"]]
        responses[BRACKET] = TradingError("opening tiers unavailable")
        api = FixtureAPI(responses)
        market = LocalQuoteMarket()
        return LiveBroker({}, market, api=api), api, market

    def test_missing_opening_tiers_or_caps_does_not_prevent_collateral_read(self):
        broker, api, _ = self.broker()
        for row in api.responses[ACCOUNT]["positions"]:
            row.pop("maxNotional")
        result = broker.margin_snapshot([SYMBOL])
        self.assertEqual((result.wallet, result.available, result.maintenance), (dec(200), dec(150), dec(5)))
        self.assertEqual(result.brackets, {})
        self.assertEqual(result.current_leverage_caps, {})
        self.assertIsNone(result.open_orders)  # A collateral read never proves no open orders.
        self.assertCountEqual([call[1] for call in api.calls], [DUAL, MULTI, ACCOUNT, RISK])
        self.assertTrue(all(call[0] == "GET" for call in api.calls))
        with self.assertRaisesRegex(TradingError, "opening tiers"):
            broker.snapshot([SYMBOL])  # The trading contract still requires tiers.

    def test_complete_exposure_and_losses_remain_part_of_collateral_risk(self):
        broker, api, _ = self.broker(account_row("SPCXUSD1", amount="2", leverage="10", entry="110", pnl="-20"))
        result = broker.margin_snapshot([SYMBOL])
        self.assertEqual(result.occupied_margin_exact, Fraction(20))
        self.assertEqual((result.wallet, result.equity, result.available), (dec(200), dec(180), dec(130)))
        self.assertIn("SPCXUSD1", {position.symbol for position in result.positions if position.qty})
        self.assertNotIn(BRACKET, [call[1] for call in api.calls])

    def test_unsynchronized_balance_and_position_or_unknown_margin_asset_still_fail(self):
        broker, api, market = self.broker()
        api.responses[RISK][0].update(positionAmt="1", entryPrice="100")
        with self.assertRaisesRegex(TradingError, "快照正在同步"):
            broker.margin_snapshot([SYMBOL])
        api.responses[RISK][0].update(positionAmt="0", entryPrice="0")
        market.assets[SYMBOL] = "USDT"
        with self.assertRaisesRegex(TradingError, "非 USD1"):
            broker.margin_snapshot([SYMBOL])

    def test_events_during_and_after_read_revoke_collateral_authority(self):
        broker, api, _ = self.broker()
        result = broker.margin_snapshot([SYMBOL])
        broker.require_snapshot_current(result)
        broker._cycle_account_event("ACCOUNT_UPDATE")
        with self.assertRaisesRegex(TradingError, "失效"):
            broker.require_snapshot_current(result)
        original = api.call

        def event_during_read(method, path, *args, **kwargs):
            response = original(method, path, *args, **kwargs)
            if path == RISK:
                broker._cycle_account_event("ACCOUNT_UPDATE")
            return response

        with patch.object(api, "call", side_effect=event_during_read):
            with self.assertRaisesRegex(TradingError, "查询期间发生变化"):
                broker.margin_snapshot([SYMBOL])

    def test_account_modes_and_staleness_are_not_relaxed(self):
        broker, api, _ = self.broker()
        broker.margin_snapshot([SYMBOL], reuse_account_mode=True)
        broker._cycle_account_event("ACCOUNT_CONFIG_UPDATE")
        api.responses[DUAL] = {"dualSidePosition": False}
        result = broker.margin_snapshot([SYMBOL], reuse_account_mode=True)
        with self.assertRaises(AccountModeError):
            result.require_modes([SYMBOL])
        result.timestamp -= 10
        with self.assertRaisesRegex(TradingError, "过期"):
            broker.require_snapshot_current(result)

    def test_margin_read_cannot_reuse_or_create_leverage_authority(self):
        broker, api, _ = self.broker()
        api.responses[BRACKET] = {"symbol": SYMBOL, "brackets": PAPER_BRACKETS}
        broker.snapshot([SYMBOL], fresh_modes=True)
        self.assertIsNotNone(broker.leverage_snapshot)
        broker.margin_snapshot([SYMBOL], fresh_modes=True)
        self.assertIsNone(broker.leverage_snapshot)

    def test_budget_estimate_excludes_only_opening_tier_reads(self):
        broker, api, _ = self.broker()
        for reuse in (False, True):
            with self.subTest(reuse=reuse):
                self.assertEqual(broker.snapshot_weight([SYMBOL], reuse_account_mode=reuse)
                                 - broker.margin_snapshot_weight([SYMBOL], reuse_account_mode=reuse), 1)
        broker.margin_snapshot([SYMBOL], reuse_account_mode=True)
        self.assertEqual(broker.margin_snapshot_weight([SYMBOL], reuse_account_mode=True), 11)
        self.assertEqual(broker.margin_snapshot_weight([SYMBOL], fresh_modes=True), 71)
        self.assertEqual(sum(call[1] == DUAL for call in api.calls), 1)

    def test_confirmation_can_force_both_mode_reads_without_opening_tiers(self):
        broker, api, _ = self.broker()
        broker.margin_snapshot([SYMBOL])
        api.calls.clear()
        api.responses[MULTI] = {"multiAssetsMargin": True}
        result = broker.margin_snapshot([SYMBOL], fresh_modes=True)
        self.assertTrue(result.multi_assets)
        self.assertCountEqual([call[1] for call in api.calls], [DUAL, MULTI, ACCOUNT, RISK])
        self.assertIsNone(broker.leverage_snapshot)
