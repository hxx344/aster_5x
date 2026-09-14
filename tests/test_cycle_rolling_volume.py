"""Rolling fill-accounting boundaries and indexed, exact history aggregation."""
from contextlib import contextmanager
from decimal import Decimal, localcontext
from fractions import Fraction
from unittest import TestCase
from unittest.mock import patch

from tests import test_cycle_volume as ledger_cases
from trading.models import TradingError, dec, wire
from trading.store import Store


DAY = ledger_cases.DAY


class CycleRollingVolumeTests(TestCase):
    setUp = ledger_cases.CycleVolumeTests.setUp
    intent = ledger_cases.CycleVolumeTests.intent
    fill = ledger_cases.CycleVolumeTests.fill

    def test_empty_window_has_stable_complete_shape_and_one_clock_anchor(self):
        now = int(DAY + 100)
        expected = {"window_start": float(now - 86400), "window_end": float(now), "volume": "0",
                    "trade_count": 0, "next_release_at": None, "estimated_volume": "0", "estimated_trade_count": 0}
        self.assertEqual(self.store.cycle_rolling_volume("first", now=now), expected)
        with patch("trading.store.time.time", return_value=now) as clock:
            actual = self.store.cycle_rolling_volume("first")
        self.assertEqual(actual, expected)
        self.assertIs(type(actual["window_start"]), float)
        self.assertIs(type(actual["window_end"]), float)
        clock.assert_called_once_with()

    def test_exact_twenty_four_hours_excluded_now_included_and_future_excluded(self):
        now = DAY + 100
        intent = self.intent()
        rows = [self.fill(intent, str(index), price=str(index), executed_at=stamp)
                for index, stamp in enumerate((now - 86400 - .001, now - 86400,
                                                now - 86400 + .001, now, now + .001), start=1)]
        self.store.record_cycle_fills(intent, rows)
        current = self.store.cycle_rolling_volume("first", now=now)
        self.assertEqual((current["volume"], current["trade_count"]), ("7", 2))
        self.assertEqual(current["next_release_at"], rows[2]["executed_at"] + 86400)

    def test_utc_midnight_keeps_previous_day_fills_in_the_rolling_window(self):
        intent = self.intent()
        self.store.record_cycle_fills(intent, [self.fill(intent, "yesterday", executed_at=DAY - 1),
                                               self.fill(intent, "today", executed_at=DAY)])
        before = self.store.cycle_rolling_volume("first", now=DAY - .001)
        after = self.store.cycle_rolling_volume("first", now=DAY)
        self.assertEqual((before["volume"], after["volume"]), ("100", "200"))
        self.assertEqual(self.store.cycle_daily_volume("first", now=DAY)["volume"], "100")
        self.assertEqual(after["next_release_at"], DAY + 86399)

    def test_next_release_advances_one_execution_boundary_at_a_time(self):
        now = DAY + 300
        intent = self.intent()
        self.store.record_cycle_fills(intent, [self.fill(intent, "first", executed_at=now - 86399),
                                               self.fill(intent, "second", executed_at=now - 50),
                                               self.fill(intent, "third", executed_at=now)])
        current = self.store.cycle_rolling_volume("first", now=now)
        self.assertEqual(current["next_release_at"], now + 1)
        released = self.store.cycle_rolling_volume("first", now=current["next_release_at"])
        self.assertEqual((released["volume"], released["trade_count"]), ("200", 2))
        self.assertEqual(released["next_release_at"], now + 86350)
        empty = self.store.cycle_rolling_volume("first", now=now + 86400)
        self.assertEqual((empty["volume"], empty["trade_count"], empty["next_release_at"]), ("0", 0, None))

    def test_account_isolation_and_all_symbols_share_the_account_window(self):
        first = self.intent()
        second = self.intent("second-account", account_id="second")
        alternate = self.intent("other-symbol")
        alternate["symbol"] = "CLUSD1"
        for order in alternate["orders"]:
            order["symbol"] = "CLUSD1"
        for receipt in alternate["receipts"].values():
            receipt["symbol"] = "CLUSD1"
        self.store.save_intent(alternate)
        self.store.record_cycle_fills(first, [self.fill(first, "same-id")])
        self.store.record_cycle_fills(second, [self.fill(second, "same-id", quantity="2")])
        self.store.record_cycle_fills(alternate, [self.fill(alternate, "same-id", quantity="3")])
        self.assertEqual(self.store.cycle_rolling_volume("first", now=DAY + 10)["volume"], "400")
        self.assertEqual(self.store.cycle_rolling_volume("second", now=DAY + 10)["volume"], "200")

    def test_late_fill_restart_and_duplicate_replay_keep_exact_window(self):
        now = DAY + 100
        intent = self.intent()
        later = self.fill(intent, "later", quantity="2", executed_at=now - 10)
        earlier = self.fill(intent, "late-arrival", executed_at=now - 600)
        expired = self.fill(intent, "already-expired", executed_at=now - 86400)
        self.store.record_cycle_fills(intent, [later])
        self.assertEqual(self.store.cycle_rolling_volume("first", now=now)["volume"], "200")
        self.store.record_cycle_fills(intent, [earlier, expired])
        reopened = Store(self.path)
        self.assertEqual(reopened.record_cycle_fills(intent, [earlier, later, expired]), 0)
        result = reopened.cycle_rolling_volume("first", now=now)
        self.assertEqual((result["volume"], result["trade_count"]), ("300", 2))
        self.assertEqual(result["next_release_at"], now - 600 + 86400)

    def test_zero_configured_limit_does_not_disable_statistics(self):
        account = self.store.account("first")
        self.assertEqual(account["cycle"]["daily_volume_limit"], "0")
        intent = self.intent()
        self.store.record_cycle_fills(intent, [self.fill(intent)])
        self.assertEqual(self.store.cycle_rolling_volume("first", now=DAY + 10)["volume"], "100")

    def test_high_precision_sum_does_not_round_under_small_decimal_context(self):
        intent = self.intent()
        tiny = self.fill(intent, "tiny", price="0.00000000000000000000000000000000000001")
        large = self.fill(intent, "large", price="1000000000000000000000000000000")
        self.store.record_cycle_fills(intent, [tiny, large])
        with localcontext() as context:
            context.prec = 6
            current = self.store.cycle_rolling_volume("first", now=DAY + 10)
        self.assertEqual(current["volume"], wire(Fraction(dec(tiny["notional"])) + Fraction(dec(large["notional"]))))
        self.assertEqual(current["trade_count"], 2)

    def test_estimated_subset_uses_the_same_time_boundaries(self):
        now = DAY + 100
        intent = self.intent()
        self.store.record_cycle_fills(intent, [
            self.fill(intent, "real", quantity="2", executed_at=now, time_source="exchange"),
            self.fill(intent, "legacy", executed_at=now - 10, time_source="legacy_estimated"),
            self.fill(intent, "expired", quantity="3", executed_at=now - 86400, time_source="legacy_estimated"),
            self.fill(intent, "future", quantity="4", executed_at=now + .001, time_source="legacy_estimated"),
        ])
        result = self.store.cycle_rolling_volume("first", now=now)
        self.assertEqual((result["volume"], result["trade_count"]), ("300", 2))
        self.assertEqual((result["estimated_volume"], result["estimated_trade_count"]), ("100", 1))

    def test_query_covers_more_than_ui_limit_and_seeks_account_time_index(self):
        intent = self.intent(quantity="50000", filled="50000")
        self.store.record_cycle_fills(intent, [self.fill(intent, "seed", executed_at=DAY - 86401)])
        with self.store.connect() as db:
            seed = dict(db.execute("SELECT * FROM cycle_fills WHERE trade_id='seed'").fetchone())
            fields = tuple(seed)
            # Seed normalized ledger rows directly: this test measures the range
            # query, while fill admission has separate transactional coverage.
            stamps = [DAY - 172800 - index for index in range(20000)]
            stamps.extend(DAY - 200 + index / 10 for index in range(1201))
            db.executemany("INSERT INTO cycle_fills(" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ")",
                           (tuple({**seed, "trade_id": "history-" + str(index), "executed_at": stamp}[field] for field in fields)
                            for index, stamp in enumerate(stamps)))
        statements = []
        original_connect = self.store.connect
        @contextmanager
        def traced_connect():
            with original_connect() as db:
                db.set_trace_callback(statements.append)
                yield db
        with patch.object(self.store, "connect", traced_connect):
            result = self.store.cycle_rolling_volume("first", now=DAY)
        self.assertEqual((result["volume"], result["trade_count"]), ("120100", 1201))
        self.assertEqual(result["next_release_at"], DAY - 200 + 86400)
        selects = [query for query in statements if "FROM cycle_fills" in query]
        self.assertEqual(len(selects), 1)
        with original_connect() as db:
            plan = " ".join(row[3] for row in db.execute("EXPLAIN QUERY PLAN " + selects[0]))
        self.assertIn("SEARCH cycle_fills USING INDEX idx_cycle_fills_recent", plan)
        self.assertIn("account_id=? AND executed_at>? AND executed_at<?", plan)
        self.assertNotIn("USE TEMP B-TREE", plan)

    def test_invalid_account_and_time_fail_explicitly_and_epoch_is_supported(self):
        for account_id in (None, "", "../first", "FIRST", True):
            with self.subTest(account_id=account_id), self.assertRaises(TradingError):
                self.store.cycle_rolling_volume(account_id, now=DAY)
        for now in (True, "123", Decimal("123"), float("nan"), float("inf"), -1, 1e30, 10 ** 1000):
            with self.subTest(now=now), self.assertRaises(TradingError):
                self.store.cycle_rolling_volume("first", now=now)
        intent = self.intent()
        self.store.record_cycle_fills(intent, [self.fill(intent, executed_at=0)])
        result = self.store.cycle_rolling_volume("first", now=0)
        self.assertEqual((result["window_start"], result["window_end"]), (-86400.0, 0.0))
        self.assertEqual((result["volume"], result["next_release_at"]), ("100", 86400.0))
