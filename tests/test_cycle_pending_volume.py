"""Delayed reporting cannot strand cycles or silently spend the daily quota."""
from copy import deepcopy
from fractions import Fraction
import json
from unittest import TestCase
from unittest.mock import Mock, patch

from tests import test_cycle_volume as volume_cases, test_cycle_hot_execution as hot_cases
from trading.cycle import DailyVolumeLimitError
from trading.engine import Engine
from trading.models import TradingError, dec, wire
from trading.store import Store


DAY = volume_cases.DAY
SYMBOL = "XAUUSD1"


class PendingVolumeTests(TestCase):
    setUp = volume_cases.CycleVolumeTests.setUp
    fill = volume_cases.CycleVolumeTests.fill

    def intent(self, **kwargs):
        intent = volume_cases.CycleVolumeTests.intent(self, **kwargs)
        for receipt in intent["receipts"].values():
            receipt["cumQuote"] = wire(Fraction(dec(receipt["executedQty"])) * 100)
        self.store.save_intent(intent)
        return intent

    def daily(self, **kwargs):
        return self.store.cycle_daily_volume("first", now=DAY + 60, symbol=SYMBOL, include_pending=True, **kwargs)

    def test_partial_backfill_replaces_reservation_once_and_survives_restart(self):
        intent = self.intent(quantity="2", filled="2")
        before = self.daily()
        self.assertEqual((before["volume"], before["reserved_volume"], before["quota_pending"]), ("0", "200", False))
        first = self.fill(intent)
        self.store.record_cycle_fills(intent, [first])
        self.store = Store(self.path)
        partial = self.daily()
        self.assertEqual((partial["volume"], partial["reserved_volume"]), ("100", "100"))
        self.store.record_cycle_fills(intent, [first, self.fill(intent, trade_id="second")])
        complete = self.daily()
        self.assertEqual((complete["volume"], complete["reserved_volume"], complete["quota_pending"]), ("200", "0", False))
        self.store.mark_cycle_volume_synced(intent["id"])
        self.assertFalse(self.daily()["sync_pending"])
        self.assertEqual(self.daily()["volume"], "200")

    def test_cumulative_quote_is_used_without_rounded_average_price(self):
        intent = self.intent(quantity="2", filled="2")
        receipt = intent["receipts"][intent["orders"][0]["newClientOrderId"]]
        receipt.update(cumQuote="200.1234567890123456789", avgPrice="100.06")
        self.store.save_intent(intent)
        self.assertEqual(self.daily()["reserved_volume"], receipt["cumQuote"])

    def test_missing_nonterminal_or_conflicting_receipts_keep_quota_unavailable(self):
        original = self.intent(quantity="2", filled="2")
        cid = original["orders"][0]["newClientOrderId"]
        for changes in ({"cumQuote": None}, {"cumQuote": "0"}, {"cumQuote": "NaN"},
                        {"status": "PARTIALLY_FILLED"}, {"clientOrderId": "wrong"}, {"executedQty": "3"}):
            with self.subTest(changes=changes):
                intent = deepcopy(original)
                intent["receipts"][cid].update(changes)
                self.store.save_intent(intent)
                self.assertTrue(self.daily()["quota_pending"])
        self.store.save_intent(original)
        self.store.record_cycle_fills(original, [self.fill(original)])
        original["receipts"][cid]["cumQuote"] = "99"
        self.store.save_intent(original)
        self.assertTrue(self.daily()["quota_pending"])

    def test_repair_amounts_count_and_other_accounts_and_symbols_do_not(self):
        intent = self.intent(quantity="2", filled="2", short_filled="2")
        order = {**intent["orders"][0], "newClientOrderId": "repair", "side": "SELL"}
        intent["repairs"].append(order)
        intent["receipts"]["repair"] = {**order, "clientOrderId": "repair", "orderId": "repair-order",
            "status": "FILLED", "executedQty": "2", "cumQuote": "210"}
        self.store.save_intent(intent)
        self.intent(intent_id="other-account", account_id="second")
        other = self.intent(intent_id="other-symbol")
        other["symbol"] = "CLUSD1"
        for row in [*other["orders"], *other["receipts"].values()]:
            row["symbol"] = "CLUSD1"
        self.store.save_intent(other)
        self.assertEqual(self.daily()["reserved_volume"], "610")

    def test_cross_midnight_missing_fills_are_reserved_until_their_days_are_known(self):
        intent = self.intent(quantity="2", filled="2", created_at=DAY - 10, completed_at=DAY + 1)
        self.assertEqual(self.daily()["reserved_volume"], "200")
        self.store.record_cycle_fills(intent, [self.fill(intent, executed_at=DAY - .001)])
        self.assertEqual((self.daily()["volume"], self.daily()["reserved_volume"]), ("0", "100"))
        self.store.record_cycle_fills(intent, [self.fill(intent, trade_id="today", executed_at=DAY + .001)])
        self.assertEqual((self.daily()["volume"], self.daily()["reserved_volume"]), ("100", "0"))
        tomorrow = self.store.cycle_daily_volume("first", now=DAY + 86400, symbol=SYMBOL, include_pending=True)
        self.assertEqual((tomorrow["volume"], tomorrow["reserved_volume"], tomorrow["sync_pending"]), ("0", "0", False))

    def test_backfill_between_reads_cannot_make_fills_and_reservation_both_disappear(self):
        intent = self.intent(quantity="1", filled="1")
        writer = Store(self.path)
        pending = self.store._cycle_pending_volume

        def finish_backfill(*args):
            writer.record_cycle_fills(intent, [self.fill(intent)])
            writer.mark_cycle_volume_synced(intent["id"])
            return pending(*args)

        with patch.object(self.store, "_cycle_pending_volume", side_effect=finish_backfill):
            daily = self.daily()
        self.assertEqual((daily["volume"], daily["reserved_volume"]), ("0", "100"))
        self.assertEqual((self.daily()["volume"], self.daily()["reserved_volume"]), ("100", "0"))

    def test_too_many_pending_batches_never_truncates_the_quota_silently(self):
        intent = self.intent()
        with self.store.connect() as db:
            for i in range(1000):
                clone = {**intent, "id": f"pending-{i}"}
                db.execute("INSERT INTO intents(id,account_id,status,data) VALUES (?,?,?,?)",
                           (clone["id"], "first", "complete", json.dumps(clone)))
                self.store._index_cycle_volume(db, clone)
        self.assertTrue(self.daily()["quota_pending"])


class LiveReportingExecutionTests(TestCase):
    setUp = hot_cases.CycleHotExecutionTests.setUp
    plan = hot_cases.CycleHotExecutionTests.plan
    progress_now = hot_cases.CycleHotExecutionTests.progress_now
    start_hot = hot_cases.CycleHotExecutionTests.start_hot

    def close(self):
        progress = self.progress_now()
        progress.update(opened_at=0, phase="waiting_close")
        self.f.store.put("cycle:test", progress)
        self.hot.publish()
        self.start_hot("close")

    def test_live_unlimited_cycles_continue_while_history_is_unavailable(self):
        self.hot.cycle_trades.side_effect = AssertionError("trade reporting must stay off the execution path")
        self.start_hot()
        self.close()
        self.hot.publish()
        self.start_hot()
        self.assertEqual(self.hot.submit.call_count, 3)
        self.assertEqual(self.progress_now()["phase"], "holding")
        self.assertEqual(len(self.f.store.cycle_volume_backlog("test")), 3)
        self.hot.cycle_trades.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))

    def test_completed_live_history_still_backfills_after_manual_pause(self):
        self.start_hot()
        self.close()
        engine = Engine(self.f.store, market=self.f.market)
        self.addCleanup(engine.dashboard_reports.close)
        engine.live_allowed = Mock(return_value=True)
        engine.brokers["test"] = self.hot
        account = self.f.store.account("test")
        account.update(mode="live", enabled=False)
        account["cycle"]["enabled"] = False
        self.f.store.save_account(account)
        before = self.hot.submit.call_count
        with patch.object(engine, "cycle_hot_ready") as ready:
            self.assertEqual(engine.poll_cycle_history("test"), 5)
            ready.assert_not_called()
        self.assertEqual(self.f.store.cycle_daily_volume("test")["trade_count"], 4)
        self.assertFalse(self.f.store.cycle_volume_backlog("test"))
        self.assertEqual(self.hot.submit.call_count, before)
        self.assertFalse(self.f.store.account("test")["enabled"])

    def test_daily_cap_uses_receipts_before_history_and_blocks_when_reserved_room_is_spent(self):
        post = self.hot.paper.submit

        def terminal_quote(orders):
            rows = post(orders)
            for row in rows:
                row["cumQuote"] = wire(Fraction(dec(row["executedQty"])) * Fraction(dec(row["avgPrice"])))
            return rows

        self.hot.paper.submit = terminal_quote
        self.f.account["cycle"]["daily_volume_limit"] = "100000"
        self.f.store.save_account(self.f.account)
        self.start_hot()
        self.close()
        daily = self.f.store.cycle_daily_volume("test", symbol=SYMBOL, include_pending=True)
        self.assertEqual(daily["volume"], "0")
        self.assertGreater(dec(daily["reserved_volume"]), 0)
        engine = Engine(self.f.store, market=self.f.market)
        self.addCleanup(engine.dashboard_reports.close)
        allowance = engine.cycle_open_allowances(self.f.account)
        self.assertEqual(dec(allowance["daily_remaining"]), dec("100000") - dec(daily["reserved_volume"]))
        self.hot.publish()
        self.start_hot()
        self.close()
        self.hot.publish()
        posts = self.hot.submit.call_count

        # Tightening the configured cap makes already reserved fills count.
        self.f.account["cycle"]["daily_volume_limit"] = daily["reserved_volume"]
        self.f.store.save_account(self.f.account)
        with self.assertRaises(DailyVolumeLimitError):
            self.start_hot()
        self.assertEqual(self.hot.submit.call_count, posts)
        self.hot.cycle_trades.assert_not_called()

    def test_unlimited_admission_does_not_read_the_reporting_ledger(self):
        engine = Engine(self.f.store, market=self.f.market)
        self.addCleanup(engine.dashboard_reports.close)
        with patch.object(self.f.store, "cycle_daily_volume", side_effect=AssertionError("statistics only")), \
             patch.object(self.f.store, "cycle_volume_backlog", side_effect=AssertionError("statistics only")):
            self.assertEqual(engine.cycle_open_allowances(self.f.account), {"daily_remaining": None})
            self.start_hot()

    def test_missing_amount_still_blocks_capped_live_open_and_reservation_is_rechecked(self):
        self.start_hot()
        self.close()
        self.hot.publish()
        self.f.account["cycle"]["daily_volume_limit"] = "100000"
        self.f.store.save_account(self.f.account)
        posts = self.hot.submit.call_count
        with self.assertRaisesRegex(TradingError, "补齐成交金额"):
            self.start_hot()
        backlog = self.f.store.cycle_volume_backlog("test")
        for intent in backlog:
            for receipt in intent["receipts"].values():
                receipt["cumQuote"] = wire(Fraction(dec(receipt["executedQty"])) * Fraction(dec(receipt["avgPrice"])))
            self.f.store.save_intent(intent)

        def change_amount(_):
            intent = backlog[0]
            next(iter(intent["receipts"].values()))["cumQuote"] = "100000"
            self.f.store.save_intent(intent)

        with self.assertRaises(DailyVolumeLimitError):
            self.start_hot(before_submit=change_amount)
        self.assertEqual(self.hot.submit.call_count, posts)
        self.assertIsNone(self.f.store.intent("test"))
