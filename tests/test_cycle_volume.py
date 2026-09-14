from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from decimal import localcontext
from fractions import Fraction
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from trading.cycle_volume import normalize_fill, utc_day
from trading.models import TradingError, dec, wire
from trading.store import Store


DAY = datetime(2026, 9, 14, tzinfo=timezone.utc).timestamp()


class CycleVolumeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "ledger.sqlite3"
        self.store = Store(self.path)
        for account_id in ("first", "second"):
            self.store.save_account({"id": account_id, "mode": "paper", "enabled": True,
                                     "policy": {"min_open_leverage": 5}})

    def intent(self, intent_id="intent-1", account_id="first", phase="open", status="complete",
               quantity="10", filled="10", short_filled="0", created_at=DAY + 1, completed_at=DAY + 30):
        orders, receipts = [], {}
        for position_side, executed in (("LONG", filled), ("SHORT", short_filled)):
            side = ("BUY" if position_side == "LONG" else "SELL") if phase == "open" else \
                   ("SELL" if position_side == "LONG" else "BUY")
            client_id = intent_id + "-" + position_side
            order = {"symbol": "XAUUSD1", "positionSide": position_side, "side": side,
                     "quantity": quantity, "newClientOrderId": client_id}
            receipt = {**order, "orderId": "order-" + client_id, "clientOrderId": client_id,
                       "executedQty": executed, "avgPrice": "100",
                       "status": "FILLED" if dec(executed) == dec(quantity) else "EXPIRED"}
            orders.append(order)
            receipts[client_id] = receipt
        result = {"id": intent_id, "account_id": account_id, "kind": "cycle", "symbol": "XAUUSD1",
                  "phase": phase, "status": status, "run_id": "run-" + account_id,
                  "created_at": created_at, "orders": orders, "repairs": [], "receipts": receipts}
        if completed_at is not None:
            result["completed_at"] = completed_at
        self.store.save_intent(result)
        return result

    def fill(self, intent, trade_id="trade-1", quantity="1", price="100", executed_at=DAY + 10,
             position_side="LONG", time_source="paper", repair=False):
        order = next(order for order in intent["repairs" if repair else "orders"] if order["positionSide"] == position_side)
        client_id = order["newClientOrderId"]
        return {"trade_id": trade_id, "order_id": str(intent["receipts"][client_id]["orderId"]),
                "client_id": client_id, "symbol": order["symbol"], "position_side": position_side,
                "side": order["side"], "quantity": quantity, "price": price,
                "notional": wire(Fraction(dec(quantity)) * Fraction(dec(price))),
                "executed_at": executed_at, "time_source": time_source}

    def test_empty_day_and_exact_utc_midnight_reset(self):
        current = self.store.cycle_daily_volume("first", now=DAY + 86399.99)
        self.assertEqual((current["utc_date"], current["volume"], current["trade_count"]), ("2026-09-14", "0", 0))
        self.assertEqual(current["next_reset_at"], DAY + 86400)
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY + 86400)["utc_date"], "2026-09-15")
        self.assertEqual(utc_day(DAY - .001)[0], "2026-09-13")

    def test_each_partial_fill_is_recorded_before_intent_finishes(self):
        intent = self.intent(status="pending", filled="2", completed_at=None)
        cid = intent["orders"][0]["newClientOrderId"]
        intent["receipts"][cid]["status"] = "PARTIALLY_FILLED"
        self.store.save_intent(intent)
        self.assertEqual(self.store.record_cycle_fills(intent, [self.fill(intent)]), 1)
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY)["volume"], "100")
        self.store.record_cycle_fills(intent, [self.fill(intent, trade_id="trade-2", executed_at=DAY + 11)])
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY)["trade_count"], 2)
        self.assertEqual(self.store.intent("first")["status"], "pending")
        with self.assertRaises(TradingError):
            self.store.mark_cycle_volume_synced(intent["id"])

    def test_duplicate_fill_and_duplicate_in_same_batch_count_once(self):
        intent = self.intent()
        fill = self.fill(intent)
        self.assertEqual(self.store.record_cycle_fills(intent, [fill, fill]), 1)
        self.assertEqual(self.store.record_cycle_fills(intent, [fill]), 0)
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY)["volume"], "100")
        self.assertEqual(len(self.store.events()), 1)

    def test_equivalent_decimal_formatting_is_idempotent(self):
        intent = self.intent()
        fill = self.fill(intent, quantity="1.00", price="100.00")
        self.store.record_cycle_fills(intent, [fill])
        fill.update(quantity="1", price="1e2", notional="1e2")
        self.assertEqual(self.store.record_cycle_fills(intent, [fill]), 0)

    def test_conflicting_trade_identity_rolls_back_other_new_rows(self):
        intent = self.intent()
        first = self.fill(intent)
        self.store.record_cycle_fills(intent, [first])
        changed = {**first, "executed_at": DAY + 12}
        with self.assertRaisesRegex(TradingError, "冲突"):
            self.store.record_cycle_fills(intent, [self.fill(intent, "trade-2"), changed])
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY)["trade_count"], 1)
        self.assertEqual(len(self.store.events()), 1)

    def test_account_scoped_trade_ids_and_records_are_isolated(self):
        first, second = self.intent(), self.intent("intent-2", account_id="second")
        self.store.record_cycle_fills(first, [self.fill(first)])
        self.store.record_cycle_fills(second, [self.fill(second, quantity="2")])
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY)["volume"], "100")
        self.assertEqual(self.store.cycle_daily_volume("second", now=DAY)["volume"], "200")
        self.assertEqual(self.store.cycle_trade_records("first")[0]["intent_id"], first["id"])
        with self.assertRaisesRegex(TradingError, "账户"):
            self.store.record_cycle_fills({**first, "account_id": "second"}, [self.fill(first, "trade-2")])

    def test_fill_must_match_persisted_order_and_receipt_identity(self):
        intent = self.intent()
        original = self.fill(intent)
        invalid = ({"client_id": "unknown"}, {"order_id": "other-order"}, {"symbol": "CLUSD1"},
                   {"side": "SELL"}, {"position_side": "SHORT"})
        for change in invalid:
            with self.subTest(change=change), self.assertRaises(TradingError):
                self.store.record_cycle_fills(intent, [{**original, **change}])
        self.assertEqual(self.store.cycle_trade_records("first"), [])
        self.assertEqual(self.store.events(), [])

    def test_mutating_only_in_memory_receipt_cannot_authorize_more_volume(self):
        intent = self.intent(filled="1")
        intent["receipts"][intent["orders"][0]["newClientOrderId"]]["executedQty"] = "3"
        with self.assertRaisesRegex(TradingError, "超过已核实回执"):
            self.store.record_cycle_fills(intent, [self.fill(intent, quantity="2")])
        self.assertEqual(self.store.cycle_trade_records("first"), [])

    def test_cumulative_fill_quantity_cannot_exceed_confirmed_partial_receipt(self):
        intent = self.intent(filled="2")
        self.store.record_cycle_fills(intent, [self.fill(intent)])
        with self.assertRaisesRegex(TradingError, "超过已核实回执"):
            self.store.record_cycle_fills(intent, [self.fill(intent, "trade-2", quantity="2")])
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY)["volume"], "100")

    def test_late_fill_recomputes_display_and_event_prefixes_in_execution_order(self):
        intent = self.intent()
        later = self.fill(intent, "trade-z", quantity="2", executed_at=DAY + 20)
        self.store.record_cycle_fills(intent, [later])
        earlier = self.fill(intent, "trade-a", quantity="1", executed_at=DAY + 10)
        self.store.record_cycle_fills(intent, [earlier])
        records = self.store.cycle_trade_records("first")
        self.assertEqual([(record["trade_id"], record["daily_volume"]) for record in records], [("trade-z", "300"), ("trade-a", "100")])
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY)["volume"], "300")
        self.assertTrue(any("本笔交易量 200" in event["message"] and "累计 300" in event["message"] for event in self.store.events()))

    def test_equal_execution_times_use_stable_trade_id_order(self):
        intent = self.intent()
        self.store.record_cycle_fills(intent, [self.fill(intent, "b", quantity="2"), self.fill(intent, "a")])
        self.assertEqual([(record["trade_id"], record["daily_volume"]) for record in self.store.cycle_trade_records("first")],
                         [("b", "300"), ("a", "100")])

    def test_late_old_day_fill_does_not_consume_current_utc_day(self):
        intent = self.intent(created_at=DAY - 10)
        self.store.record_cycle_fills(intent, [self.fill(intent, "old", executed_at=DAY - .001),
                                               self.fill(intent, "new", executed_at=DAY)])
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY - 1)["volume"], "100")
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY)["volume"], "100")
        self.assertEqual({record["utc_date"] for record in self.store.cycle_trade_records("first")}, {"2026-09-13", "2026-09-14"})

    def test_reopen_preserves_amounts_prefixes_and_idempotency(self):
        intent = self.intent()
        self.store.record_cycle_fills(intent, [self.fill(intent)])
        reopened = Store(self.path)
        self.assertEqual(reopened.record_cycle_fills(intent, [self.fill(intent)]), 0)
        self.assertEqual(reopened.cycle_daily_volume("first", now=DAY)["volume"], "100")
        self.assertEqual(reopened.cycle_trade_records("first")[0]["daily_volume"], "100")

    def test_open_close_and_repair_all_count_gross_executed_amount(self):
        opening = self.intent(filled="1", short_filled="1", quantity="1")
        self.store.record_cycle_fills(opening, [self.fill(opening), self.fill(opening, "short-open", position_side="SHORT")])
        closing = self.intent("close", phase="close", quantity="1", filled="1", short_filled="0")
        self.store.record_cycle_fills(closing, [self.fill(closing, "long-close")])
        repair = {"symbol": "XAUUSD1", "positionSide": "SHORT", "side": "BUY", "quantity": "1", "newClientOrderId": "repair-short"}
        closing["repairs"].append(repair)
        closing["receipts"]["repair-short"] = {**repair, "clientOrderId": "repair-short", "orderId": "repair-order",
                                                "status": "FILLED", "executedQty": "1", "avgPrice": "100"}
        self.store.save_intent(closing)
        self.store.record_cycle_fills(closing, [self.fill(closing, "short-repair", position_side="SHORT", repair=True)])
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY)["volume"], "400")
        self.assertEqual({record["phase"] for record in self.store.cycle_trade_records("first")}, {"open", "close", "repair"})

    def test_legacy_estimated_time_is_explicit_and_separately_totalled(self):
        intent = self.intent()
        self.store.record_cycle_fills(intent, [self.fill(intent, "legacy", time_source="legacy_estimated"),
                                               self.fill(intent, "actual", quantity="2", executed_at=DAY + 11, time_source="exchange")])
        totals = self.store.cycle_daily_volume("first", now=DAY)
        self.assertEqual((totals["volume"], totals["estimated_volume"], totals["estimated_trade_count"]), ("300", "100", 1))
        self.assertEqual(self.store.cycle_trade_records("first")[1]["time_source"], "legacy_estimated")
        self.assertTrue(any("时间为估算" in event["message"] for event in self.store.events()))

    def test_high_precision_totals_are_independent_of_decimal_context(self):
        intent = self.intent()
        first = self.fill(intent, price="0.00000000000000000000000000000000000001")
        second = self.fill(intent, "big", price="1000000000000000000000000000000", executed_at=DAY + 11)
        with localcontext() as context:
            context.prec = 6
            self.store.record_cycle_fills(intent, [first, second])
        expected = wire(Fraction(dec(first["notional"])) + Fraction(dec(second["notional"])))
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY)["volume"], expected)

    def test_invalid_fill_shapes_values_times_and_notional_are_rejected(self):
        intent = self.intent()
        good = self.fill(intent)
        invalid = [None, {}, {**good, "quantity": 1}, {**good, "quantity": "0"}, {**good, "price": "NaN"},
                   {**good, "notional": "101"}, {**good, "executed_at": True}, {**good, "executed_at": float("nan")},
                   {**good, "executed_at": -1}, {**good, "executed_at": 1e30}, {**good, "time_source": {}},
                   {**good, "time_source": "local"}, {**good, "trade_id": "\nsecret"}, {**good, "account_id": "second"}]
        for fill in invalid:
            with self.subTest(fill=fill), self.assertRaises(TradingError):
                self.store.record_cycle_fills(intent, [fill])
        self.assertEqual(self.store.cycle_trade_records("first"), [])

    def test_same_exchange_order_cannot_be_reassigned_to_another_intent(self):
        first = self.intent()
        self.store.record_cycle_fills(first, [self.fill(first)])
        other = self.intent("other")
        other["receipts"][other["orders"][0]["newClientOrderId"]]["orderId"] = first["receipts"][first["orders"][0]["newClientOrderId"]]["orderId"]
        self.store.save_intent(other)
        with self.assertRaisesRegex(TradingError, "订单号"):
            self.store.record_cycle_fills(other, [self.fill(other, "another-trade")])

    def test_ordinary_intent_cannot_write_cycle_ledger(self):
        intent = self.intent()
        intent["kind"] = "pair"
        self.store.save_intent(intent)
        with self.assertRaises(TradingError):
            self.store.record_cycle_fills(intent, [self.fill(intent)])

    def test_record_transaction_rolls_back_fill_event_and_total_together(self):
        intent = self.intent()
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER deny_day BEFORE INSERT ON cycle_volume_days BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.record_cycle_fills(intent, [self.fill(intent)])
        self.assertEqual(self.store.cycle_trade_records("first"), [])
        self.assertEqual(self.store.events(), [])
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY)["volume"], "0")

    def test_concurrent_duplicate_admission_is_atomic_across_store_instances(self):
        intent = self.intent()
        other = Store(self.path)
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(store.record_cycle_fills, deepcopy(intent), [self.fill(intent)]) for store in (self.store, other)]
            self.assertEqual(sum(call.result() for call in calls), 1)
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY)["trade_count"], 1)

    def test_mark_synced_requires_full_fill_coverage_and_never_rewrites_intent(self):
        intent = self.intent(quantity="2", filled="2")
        self.store.record_cycle_fills(intent, [self.fill(intent)])
        with self.assertRaisesRegex(TradingError, "全部入账"):
            self.store.mark_cycle_volume_synced(intent["id"])
        self.store.record_cycle_fills(intent, [self.fill(intent, "trade-2")])
        with self.store.connect() as db:
            before = tuple(db.execute("SELECT status,data FROM intents WHERE id=?", (intent["id"],)).fetchone())
        self.store.mark_cycle_volume_synced(intent["id"])
        self.store.mark_cycle_volume_synced(intent["id"])
        with self.store.connect() as db:
            after = tuple(db.execute("SELECT status,data FROM intents WHERE id=?", (intent["id"],)).fetchone())
        self.assertEqual(after, before)
        self.assertEqual(self.store.cycle_volume_backlog("first", since=DAY), [])

    def test_backlog_is_bounded_account_scoped_and_covers_cross_day_confirmation(self):
        today = self.intent("today", filled="0", created_at=DAY + 2, completed_at=DAY + 30)
        crossing = self.intent("cross", filled="0", created_at=DAY - 100, completed_at=DAY + 5)
        old = self.intent("old", filled="0", created_at=DAY - 10000, completed_at=DAY - 9999)
        missing = self.intent("missing", filled="0", created_at=DAY - 10000, completed_at=None)
        self.intent("pending", status="pending", filled="0", completed_at=None)
        self.intent("other-account", account_id="second", filled="0")
        with patch("trading.cycle_volume.time.time", return_value=DAY + 100):
            rows = self.store.cycle_volume_backlog("first")
            self.assertEqual({row["id"] for row in rows}, {today["id"], crossing["id"], missing["id"]})
            self.assertEqual(len(self.store.cycle_volume_backlog("first", limit=1)), 1)
        self.assertIn(old["id"], {row["id"] for row in self.store.cycle_volume_backlog("first", since=DAY - 20000)})
        self.store.mark_cycle_volume_synced(today["id"])
        self.assertNotIn(today["id"], {row["id"] for row in self.store.cycle_volume_backlog("first", since=DAY)})

    def test_upgrade_builds_index_once_and_preserves_historical_intent_json(self):
        intent = self.intent("historical", filled="0")
        with self.store.connect() as db:
            raw = db.execute("SELECT data FROM intents WHERE id=?", (intent["id"],)).fetchone()[0]
            db.execute("DROP TABLE cycle_volume_sync")
        reopened = Store(self.path)
        self.assertEqual(reopened.cycle_volume_backlog("first", since=DAY)[0]["id"], intent["id"])
        with reopened.connect() as db:
            self.assertEqual(db.execute("SELECT data FROM intents WHERE id=?", (intent["id"],)).fetchone()[0], raw)
        reopened.mark_cycle_volume_synced(intent["id"])
        self.assertEqual(Store(self.path).cycle_volume_backlog("first", since=DAY), [])

    def test_missing_original_orders_cannot_be_marked_as_zero_volume(self):
        intent = self.intent(filled="0")
        intent["orders"] = []
        self.store.save_intent(intent)
        with self.assertRaisesRegex(TradingError, "不能推断零成交"):
            self.store.mark_cycle_volume_synced(intent["id"])

    def test_complete_cycle_updates_volume_metadata_in_same_transaction(self):
        intent = self.intent(status="pending", filled="0", completed_at=None)
        progress = {"run_id": intent["run_id"], "phase": "waiting_open"}
        self.store.put("cycle:first", progress)
        self.store.complete_cycle(intent, progress)
        self.assertEqual(self.store.cycle_volume_backlog("first", since=DAY)[0]["id"], intent["id"])
        self.store.mark_cycle_volume_synced(intent["id"])
        self.assertIsNone(self.store.intent("first"))

    def test_fill_state_cannot_be_claimed_by_anonymous_demo(self):
        intent = self.intent()
        self.store.record_cycle_fills(intent, [self.fill(intent)])
        with self.store.connect() as db:
            for table in ("accounts", "intents", "events", "cycle_volume_days", "cycle_volume_sync"):
                db.execute("DELETE FROM " + table)
        with self.assertRaisesRegex(TradingError, "用途未确认"):
            Store(self.path, demo=True)
        self.assertIsNone(self.store.get("runtime_mode"))

    def test_new_empty_volume_tables_do_not_prevent_demo_binding(self):
        path = self.path.parent / "empty.sqlite3"
        fresh = Store(path)
        fresh.bind_runtime_mode(demo=True)
        self.assertEqual(Store(path, demo=True).get("runtime_mode"), "demo")

    def test_invalid_query_limits_and_timestamps_fail_explicitly(self):
        for invalid in (True, 0, -1, 1001, 1.5):
            for query in (self.store.cycle_trade_records, self.store.cycle_volume_backlog):
                with self.subTest(query=query, value=invalid), self.assertRaises(TradingError):
                    query("first", limit=invalid)
        for invalid in (True, "123", float("nan"), -1):
            with self.subTest(value=invalid), self.assertRaises(TradingError):
                self.store.cycle_daily_volume("first", now=invalid)


if __name__ == "__main__":
    unittest.main()
