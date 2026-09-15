"""Per-symbol quotas, historical prefixes and upgrade recovery stay isolated."""
import unittest
from unittest.mock import patch

from tests import test_cycle_volume as volume_cases
from tests.helpers import account as account_fixture
from trading.cycle import CyclePlan, DEFAULT_CYCLE, DailyVolumeLimitError
from trading.cycle_execution import CycleExecutor
from trading.engine import Engine
from trading.models import TradingError, dec
from trading.store import Store


DAY = volume_cases.DAY


class CycleSymbolVolumeTests(unittest.TestCase):
    setUp = volume_cases.CycleVolumeTests.setUp
    fill = volume_cases.CycleVolumeTests.fill
    intent = volume_cases.CycleVolumeTests.intent

    def add(self, symbol, token, amount, when=DAY + 10, *, sync=True):
        intent = self.intent(intent_id=token, quantity="1", filled="1")
        intent["symbol"] = symbol
        for row in [*intent["orders"], *intent["receipts"].values()]:
            row["symbol"] = symbol
        self.store.save_intent(intent)
        self.store.record_cycle_fills(intent, [self.fill(intent, trade_id=token, price=amount, executed_at=when)])
        if sync:
            self.store.mark_cycle_volume_synced(intent["id"])
        return intent

    def test_interleaved_symbols_have_independent_day_rolling_and_trade_prefixes(self):
        self.add("XAUUSD1", "xau-1", "100")
        self.add("CLUSD1", "cl-1", "900", DAY + 11)
        self.add("XAUUSD1", "xau-2", "200", DAY + 12)
        for symbol, expected in (("XAUUSD1", "300"), ("CLUSD1", "900"), ("SPCXUSD1", "0")):
            self.assertEqual(self.store.cycle_daily_volume("first", DAY + 20, symbol=symbol)["volume"], expected)
            self.assertEqual(self.store.cycle_rolling_volume("first", DAY + 20, symbol=symbol)["volume"], expected)
            self.assertEqual(self.store.cycle_daily_volume("second", DAY + 20, symbol=symbol)["volume"], "0")
        prefix = {row["trade_id"]: row["daily_volume"] for row in self.store.cycle_trade_records("first")}
        self.assertEqual(prefix, {"xau-1": "100", "xau-2": "300", "cl-1": "900"})
        self.add("XAUUSD1", "late-xau", "50", DAY + 9)
        prefix = {row["trade_id"]: row["daily_volume"] for row in self.store.cycle_trade_records("first")}
        self.assertEqual(prefix, {"late-xau": "50", "xau-1": "150", "xau-2": "350", "cl-1": "900"})

    def test_upgrade_rebuilds_existing_history_by_symbol_once_without_duplicate_fills(self):
        self.add("XAUUSD1", "xau-1", "100")
        self.add("CLUSD1", "cl-1", "900", DAY + 11)
        with self.store.connect() as db:
            db.execute("DROP TABLE cycle_symbol_volume_days")
            db.execute("UPDATE cycle_fills SET daily_volume='1000' WHERE symbol='CLUSD1'")
        migrated = Store(self.path)
        self.assertEqual(migrated.cycle_daily_volume("first", DAY + 20, symbol="CLUSD1")["volume"], "900")
        self.assertEqual(migrated.cycle_trade_records("first")[0]["daily_volume"], "900")
        self.assertEqual(len(migrated.cycle_trade_records("first")), 2)
        self.assertEqual(Store(self.path).cycle_daily_volume("first", DAY + 20, symbol="XAUUSD1")["volume"], "100")

    def test_other_symbol_quota_and_unsynced_history_do_not_block_selected_symbol(self):
        self.add("XAUUSD1", "xau-full", "1000", sync=False)
        account = account_fixture("first")
        account["cycle"] = {**DEFAULT_CYCLE, "enabled": True, "symbol": "CLUSD1", "daily_volume_limit": "500"}
        self.store.save_account(account)
        executor = CycleExecutor(self.store, None, None)
        plan = CyclePlan("open", "CLUSD1", dec(1), 5, dec(50), dec(50), dec(0))
        with patch("trading.cycle_execution.time.time", return_value=DAY + 50):
            executor._require_daily_room(account, plan, account["cycle"])
            self.add("CLUSD1", "cl-fill", "400")
            with self.assertRaises(DailyVolumeLimitError):
                executor._require_daily_room(account, plan, account["cycle"])
            executor._require_daily_room(account, CyclePlan("close", "CLUSD1", dec(1), 5, dec(50), dec(50), dec(0)), account["cycle"])

    def test_backlog_filter_happens_before_limit(self):
        self.add("XAUUSD1", "xau-missing", "100", sync=False)
        self.add("CLUSD1", "cl-missing", "100", sync=False)
        for symbol in ("XAUUSD1", "CLUSD1"):
            backlog = self.store.cycle_volume_backlog("first", limit=1, since=DAY, symbol=symbol)
            self.assertEqual(len(backlog), 1)
            self.assertEqual(backlog[0]["symbol"], symbol)
        self.assertEqual(self.store.cycle_volume_backlog("first", limit=1, since=DAY, symbol="SPCXUSD1"), [])

    def test_switching_back_restores_that_symbols_used_quota_and_cost_scope(self):
        self.add("XAUUSD1", "xau-fill", "100")
        self.add("CLUSD1", "cl-fill", "400")
        account = account_fixture("first")
        account["cycle"] = {**DEFAULT_CYCLE, "enabled": True, "daily_volume_limit": "500"}
        account["enabled"] = False
        self.store.save_account(account)
        other = account_fixture("second")
        other["enabled"] = False
        self.store.save_account(other)
        engine = Engine(self.store)
        with patch("trading.engine.time.time", return_value=DAY + 50):
            for symbol, used, fee in (("XAUUSD1", "100", "0.0125"), ("CLUSD1", "400", "0.05"), ("XAUUSD1", "100", "0.0125")):
                engine.configure("first", {"cycle": {"symbol": symbol}})
                state = next(row for row in engine.state()["accounts"] if row["id"] == "first")["cycle_state"]
                self.assertEqual(state["daily_volume"]["symbol"], symbol)
                self.assertEqual(state["daily_volume"]["volume"], used)
                self.assertEqual(state["daily_volume"]["cost"]["taker_fee"], fee)
                self.assertEqual(state["volume_by_symbol"]["CLUSD1"]["rolling_volume"]["volume"], "400")

    def test_invalid_symbol_never_falls_back_to_whole_account_quota(self):
        for symbol in ("", "BTCUSDT", [], 1):
            for method in (self.store.cycle_daily_volume, self.store.cycle_rolling_volume, self.store.cycle_volume_backlog):
                with self.subTest(symbol=symbol, method=method.__name__), self.assertRaises(TradingError):
                    method("first", symbol=symbol)
