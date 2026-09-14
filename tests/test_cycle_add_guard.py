"""Ordinary additions are isolated from only the caller account's cycle market."""
from copy import deepcopy
from unittest import TestCase

from trading.cycle_guard import ordinary_add_block_reason, ordinary_add_blocks
from trading.models import SYMBOLS, TradingError


class CycleAddGuardTests(TestCase):
    def account(self, account_id="cycling", symbol="XAUUSD1", enabled=True):
        return {"id": account_id, "enabled": True,
                "cycle": {"enabled": enabled, "symbol": symbol}}

    def test_only_selected_market_is_blocked_with_a_stable_account_specific_reason(self):
        for selected in SYMBOLS:
            with self.subTest(selected=selected):
                account = self.account(symbol=selected)
                expected = f"本账户 {selected} 已启用成交量循环，禁止该品种 5x / 10x / 20x 普通加仓"
                for symbol in SYMBOLS:
                    self.assertEqual(ordinary_add_block_reason(account, symbol), expected if symbol == selected else None)
                self.assertEqual(ordinary_add_blocks(account), {selected: expected})

    def test_different_accounts_with_the_same_market_are_independent(self):
        cycling = self.account("first")
        ordinary = self.account("second", enabled=False)
        different_cycle = self.account("third", symbol="CLUSD1")
        self.assertIsNotNone(ordinary_add_block_reason(cycling, "XAUUSD1"))
        self.assertIsNone(ordinary_add_block_reason(ordinary, "XAUUSD1"))
        self.assertIsNone(ordinary_add_block_reason(different_cycle, "XAUUSD1"))
        self.assertEqual(ordinary_add_blocks(ordinary), {})
        self.assertEqual(set(ordinary_add_blocks(different_cycle)), {"CLUSD1"})

    def test_account_pause_and_volume_wait_states_do_not_remove_selected_mode_protection(self):
        account = self.account()
        expected = ordinary_add_block_reason(account, "XAUUSD1")
        account["enabled"] = False
        for phase in ("paused", "daily_limit", "rolling_limit", "waiting_open", "holding", "waiting_close"):
            with self.subTest(phase=phase):
                account["cycle_state"] = {"phase": phase, "daily_volume": {"reached": True},
                                          "rolling_volume": {"reached": True}}
                account["cycle"]["daily_volume_limit"] = "1"
                self.assertEqual(ordinary_add_block_reason(account, "XAUUSD1"), expected)

    def test_explicit_cycle_disable_releases_protection_and_reenable_restores_it(self):
        account = self.account()
        expected = ordinary_add_block_reason(account, "XAUUSD1")
        account["cycle"]["enabled"] = False
        self.assertIsNone(ordinary_add_block_reason(account, "XAUUSD1"))
        self.assertEqual(ordinary_add_blocks(account), {})
        account["cycle"]["enabled"] = True
        self.assertEqual(ordinary_add_block_reason(account, "XAUUSD1"), expected)

    def test_leverage_and_policy_do_not_bypass_or_expand_account_cycle_protection(self):
        account = self.account()
        expected = ordinary_add_block_reason(account, "XAUUSD1")
        for leverage in (0, 1, 2, 5, 10, 20, 125):
            with self.subTest(leverage=leverage):
                account["cycle"]["leverage"] = leverage
                account["policy"] = {"symbols": list(SYMBOLS), "min_open_leverage": leverage}
                self.assertEqual(ordinary_add_block_reason(account, "XAUUSD1"), expected)
                self.assertIsNone(ordinary_add_block_reason(account, "CLUSD1"))

    def test_legacy_missing_cycle_and_selector_defaults_match_existing_configuration(self):
        for account in ({}, {"cycle": None}, {"cycle": {}}, {"cycle": {"symbol": "CLUSD1"}}):
            with self.subTest(account=account):
                self.assertEqual(ordinary_add_blocks(account), {})
                self.assertIsNone(ordinary_add_block_reason(account, "XAUUSD1"))
        account = {"cycle": {"enabled": True}}
        self.assertEqual(set(ordinary_add_blocks(account)), {"XAUUSD1"})
        self.assertIsNotNone(ordinary_add_block_reason(account, "XAUUSD1"))

    def test_invalid_account_or_cycle_selector_fails_closed_only_for_that_call(self):
        invalid_accounts = [None, [], "account", {"cycle": False}, {"cycle": []}, {"cycle": "on"}]
        invalid_accounts += [{"cycle": {"enabled": value}} for value in (None, 0, 1, "true", "false", [])]
        invalid_accounts += [{"cycle": {"enabled": enabled, "symbol": symbol}}
                             for enabled in (True, False)
                             for symbol in (None, [], 1, "UNKNOWN", "xauusd1", "")]
        for account in invalid_accounts:
            with self.subTest(account=account):
                with self.assertRaisesRegex(TradingError, "本账户.*普通加仓"):
                    ordinary_add_block_reason(account, "XAUUSD1")
                with self.assertRaises(TradingError):
                    ordinary_add_blocks(account)
                self.assertIsNone(ordinary_add_block_reason(self.account("other", enabled=False), "XAUUSD1"))

    def test_unknown_target_market_is_not_silently_accepted(self):
        for symbol in (None, [], {}, True, "UNKNOWN", "xauusd1"):
            with self.subTest(symbol=symbol), self.assertRaisesRegex(TradingError, "本账户.*普通加仓"):
                ordinary_add_block_reason(self.account(), symbol)

    def test_inputs_and_previous_result_do_not_change_later_decisions(self):
        account = self.account()
        before = deepcopy(account)
        result = ordinary_add_blocks(account)
        expected = deepcopy(result)
        result.clear()
        self.assertEqual(ordinary_add_blocks(account), expected)
        self.assertEqual(account, before)
