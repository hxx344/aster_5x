import copy
import time
import unittest
from datetime import datetime, timezone
from fractions import Fraction
from types import SimpleNamespace
from unittest.mock import Mock, patch

from trading.cycle import CyclePlan, DEFAULT_CYCLE, DailyVolumeLimitError, RollingVolumeLimitError
from trading.cycle_execution import CycleExecutor
from trading.models import TradingError, dec, wire
from .helpers import Fixture


SYMBOL = "XAUUSD1"


class RollingCycleExecutionTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.account["cycle"] = {**DEFAULT_CYCLE, "enabled": True, "daily_volume_limit": "40000"}
        self.f.store.save_account(self.f.account)
        self.progress = {"run_id": "rolling-run", "phase": "waiting_open", "quantities": {"LONG": "0", "SHORT": "0"},
                         "opened_at": None, "completed_cycles": 0, "config": copy.deepcopy(self.f.account["cycle"])}
        self.f.store.put("cycle:test", self.progress)
        self.f.broker.set_cycle_leverage(SYMBOL, 2)
        self.executor = CycleExecutor(self.f.store, self.f.broker, self.f.market)
        self.serial = 0

    def plan(self, phase="open"):
        book = self.f.market.book(SYMBOL)
        return CyclePlan(phase, SYMBOL, dec(2), 2, dec(2) * book.ask, dec(2) * book.bid, dec("0.02"))

    def open(self, callback=None):
        return self.executor.start(self.f.account, self.f.broker.cycle_snapshot([SYMBOL]), self.plan(),
                                   self.f.store.get("cycle:test"), before_submit=callback)

    def historical_fill(self, volume, stamp, *, synced=True):
        self.serial += 1
        cid = "rolling-history-" + str(self.serial)
        qty = wire(Fraction(dec(volume)) / 100)
        order = self.executor.order(SYMBOL, "LONG", "BUY", dec(qty), cid)
        receipt = {"symbol": SYMBOL, "positionSide": "LONG", "side": "BUY", "clientOrderId": cid,
                   "orderId": cid, "status": "FILLED", "executedQty": qty, "avgPrice": "100"}
        intent = {"id": cid, "account_id": "test", "kind": "cycle", "phase": "open", "symbol": SYMBOL,
                  "run_id": cid, "status": "complete", "created_at": stamp - 1, "completed_at": stamp + .1,
                  "orders": [order], "repairs": [], "receipts": {cid: receipt}}
        self.f.store.save_intent(intent)
        if synced:
            fill = {"trade_id": cid, "order_id": cid, "client_id": cid, "symbol": SYMBOL,
                    "position_side": "LONG", "side": "BUY", "quantity": qty, "price": "100",
                    "notional": str(volume), "executed_at": stamp, "time_source": "exchange"}
            self.f.store.record_cycle_fills(intent, [fill])
            self.f.store.mark_cycle_volume_synced(cid)
        return intent

    def require_room(self, now):
        with patch("trading.cycle_execution.time", SimpleNamespace(time=lambda: now)):
            self.executor._require_daily_room(self.f.account, self.plan(), self.f.account["cycle"])

    def test_utc_reset_does_not_bypass_previous_days_rolling_volume(self):
        midnight = datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp()
        self.historical_fill("10000", midnight - 1)
        self.assertEqual(self.f.store.cycle_daily_volume("test", now=midnight + 1)["volume"], "0")
        with self.assertRaises(RollingVolumeLimitError):
            self.require_room(midnight + 1)

    def test_fill_releases_exactly_at_twenty_four_hours(self):
        executed_at = datetime(2026, 9, 14, 4, tzinfo=timezone.utc).timestamp()
        self.historical_fill("10000", executed_at)
        with self.assertRaises(RollingVolumeLimitError):
            self.require_room(executed_at + 86400 - .001)
        self.require_room(executed_at + 86400)
        self.assertEqual(self.f.store.cycle_rolling_volume("test", now=executed_at + 86400)["volume"], "0")

    def test_both_windows_and_backlog_share_one_guard_timestamp(self):
        now = datetime(2026, 9, 15, 1, tzinfo=timezone.utc).timestamp()
        with patch.object(self.f.store, "cycle_daily_volume", wraps=self.f.store.cycle_daily_volume) as daily, \
             patch.object(self.f.store, "cycle_rolling_volume", wraps=self.f.store.cycle_rolling_volume) as rolling, \
             patch.object(self.f.store, "cycle_volume_backlog", wraps=self.f.store.cycle_volume_backlog) as backlog:
            self.require_room(now)
        daily.assert_called_once_with("test", now=now, symbol=SYMBOL)
        rolling.assert_called_once_with("test", now=now, symbol=SYMBOL)
        backlog.assert_called_once_with("test", limit=1, since=now - 86400, symbol=SYMBOL)

    def test_daily_limit_remains_a_distinct_error_when_both_windows_exceed(self):
        midnight = datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp()
        self.historical_fill("10000", midnight + 1)
        with self.assertRaises(DailyVolumeLimitError) as caught:
            self.require_room(midnight + 2)
        self.assertIs(type(caught.exception), DailyVolumeLimitError)

    def test_tighter_rolling_window_is_reported_when_both_windows_are_full(self):
        midnight = datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp()
        self.historical_fill("10000", midnight - 1)
        self.historical_fill("10000", midnight + 1)
        with self.assertRaises(RollingVolumeLimitError):
            self.require_room(midnight + 2)

    def test_yesterdays_unreconciled_fill_blocks_even_unlimited_opening(self):
        midnight = datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp()
        self.historical_fill("10000", midnight - 1, synced=False)
        self.f.account["cycle"]["daily_volume_limit"] = "0"
        with self.assertRaisesRegex(TradingError, "明细尚未补齐"):
            self.require_room(midnight + 1)

    def test_rolling_ledger_failure_cannot_bypass_unlimited_opening(self):
        self.f.account["cycle"]["daily_volume_limit"] = "0"
        with patch.object(self.f.store, "cycle_rolling_volume", side_effect=TradingError("rolling ledger unavailable")), \
             self.assertRaisesRegex(TradingError, "rolling ledger"):
            self.require_room(time.time())

    def test_late_yesterday_fill_at_submit_callback_blocks_the_second_guard(self):
        now = time.time()
        midnight = datetime.fromtimestamp(now, timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        def late_fill(snapshot):
            self.historical_fill("10000", midnight - 1)
        with patch.object(self.f.broker, "submit", side_effect=AssertionError("must not submit")), \
             self.assertRaises(RollingVolumeLimitError):
            self.open(late_fill)
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.f.store.cycle_daily_volume("test", now=now)["volume"], "0")
        self.assertEqual(dec(self.f.store.cycle_rolling_volume("test", now=now)["volume"]), 10000)

    def test_midnight_during_submit_callback_still_checks_rolling_usage(self):
        midnight = datetime(2026, 9, 15, tzinfo=timezone.utc).timestamp()
        clock = Mock(side_effect=[midnight - 1, midnight + 1])
        def just_before_midnight(snapshot):
            self.historical_fill("10000", midnight - .5)
        with patch("trading.cycle_execution.time", SimpleNamespace(time=clock)), \
             patch.object(self.f.broker, "submit", side_effect=AssertionError("must not submit")), \
             self.assertRaises(RollingVolumeLimitError):
            self.open(just_before_midnight)
        self.assertEqual(clock.call_count, 2)
        self.assertIsNone(self.f.store.intent("test"))

    def test_close_does_not_read_or_require_volume_room(self):
        self.open()
        progress = self.f.store.get("cycle:test")
        progress.update(phase="waiting_close", opened_at=time.time() - 61)
        self.f.store.put("cycle:test", progress)
        self.f.account["cycle"]["daily_volume_limit"] = "1"
        self.f.store.save_account(self.f.account)
        with patch.object(self.f.store, "cycle_daily_volume", side_effect=AssertionError("closing must not need daily room")), \
             patch.object(self.f.store, "cycle_rolling_volume", side_effect=AssertionError("closing must not need rolling room")):
            self.executor.start(self.f.account, self.f.broker.cycle_snapshot([SYMBOL]), self.plan("close"), progress)
        self.assertEqual(tuple(position.qty for position in self.f.broker.cycle_snapshot([SYMBOL]).pair(SYMBOL)), (0, 0))

    def test_partial_open_repair_never_checks_rolling_room_again(self):
        original_submit, original_rolling = self.f.broker.submit, self.f.store.cycle_rolling_volume
        submitted = []
        def restricted_rolling(*args, **kwargs):
            if submitted:
                raise AssertionError("repairs must not need rolling room")
            return original_rolling(*args, **kwargs)
        def one_leg(orders):
            submitted.append(True)
            return [original_submit(orders[:1])[0], {"code": -2019}] if len(orders) == 2 else original_submit(orders)
        with patch.object(self.f.broker, "submit", side_effect=one_leg) as submit, \
             patch.object(self.f.store, "cycle_rolling_volume", side_effect=restricted_rolling):
            self.open()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual(tuple(position.qty for position in self.f.broker.cycle_snapshot([SYMBOL]).pair(SYMBOL)), (0, 0))


if __name__ == "__main__":
    unittest.main()
