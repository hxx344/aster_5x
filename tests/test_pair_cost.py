"""Exact, isolated, incremental paired-cycle reporting without exchange reads."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import localcontext
from fractions import Fraction
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch

from trading import pair_cost
from trading.store import Store, dumps


NOW = datetime(2026, 9, 30, 12, tzinfo=timezone.utc).timestamp()
PAIR = {"id": "gold", "symbol": "XAUUSD1", "long_account_id": "long", "short_account_id": "short"}


def batch(identifier="batch", *, created=None, phase="open", quantity="2", buy="100.5", sell="99.5"):
    created = NOW - 10 if created is None else created
    rows = []
    for side in ("long", "short"):
        action = "BUY" if (side == "long") == (phase == "open") else "SELL"
        price = buy if action == "BUY" else sell
        cid = identifier + "-" + side
        order = {"symbol": PAIR["symbol"], "positionSide": side.upper(), "side": action,
                 "type": "MARKET", "quantity": quantity, "newClientOrderId": cid}
        receipt = {"symbol": PAIR["symbol"], "positionSide": side.upper(), "side": action,
                   "clientOrderId": cid, "status": "FILLED", "executedQty": quantity,
                   "avgPrice": price, "cumQuote": pair_cost.number(Fraction(quantity) * Fraction(price)),
                   "updateTime": int((created + 1) * 1000)}
        rows.append({"key": side, "order": order, "receipt": receipt})
    return {"id": identifier, "pair_id": "gold", "kind": "cycle", "phase": phase, "symbol": PAIR["symbol"],
            "quantity": quantity, "created_at": created, "finished_at": created + 2,
            "identities": {side: {"account_id": side, "side": side.upper()} for side in ("long", "short")},
            "legs": rows, "repairs": [], "execution_quality": {
                "scope": "pair", "pair_id": "gold", "intent_id": "pair:" + identifier,
                "symbol": PAIR["symbol"], "phase": phase,
                "final_estimate": {"status": "available", "quantity": quantity,
                                   "buy_vwap": "100.25", "sell_vwap": "99.75"}}}


class PairCostTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Store(Path(directory.name) / "state.sqlite3")

    def save(self, value):
        self.store.put("pair_batch:" + value["id"], value)

    def report(self, *, now=NOW, pair=None, pending=None, reader=None):
        if reader is not None:
            return pair_cost.read(reader, pair or PAIR, {"pending": pending}, now=now)
        with self.store.read_snapshot() as current:
            return self.report(now=now, pair=pair, pending=pending, reader=current)

    def test_open_and_close_fees_spread_and_slippage_are_not_double_counted(self):
        self.save(batch("open"))
        self.save(batch("close", phase="close"))
        result = self.report()
        for period in (result["daily"], result["weekly"]):
            self.assertEqual(period["estimated_fee"], "0.1")
            self.assertEqual(period["spread_cost"], "4")
            self.assertEqual(period["slippage_cost"], "2")
            self.assertEqual(period["total_cost"], "4.1")
            self.assertEqual(period["fill_count"], 4)
            self.assertTrue(period["complete"])
            self.assertTrue(period["slippage_complete"])
        self.assertEqual(result["fee_rate_percent"], "0.0125")
        self.assertNotIn("identities", json.dumps(result))

    def test_fraction_calculations_ignore_ambient_decimal_precision(self):
        row = batch(quantity="0.123456789123456789", buy="100.123456789", sell="99.987654321")
        self.save(row)
        with localcontext() as context:
            context.prec = 4
            result = self.report()["daily"]
        quotes = [Fraction(leg["receipt"]["cumQuote"]) for leg in row["legs"]]
        self.assertEqual(result["total_cost"], pair_cost.number(sum(quotes) / 8000 + quotes[0] - quotes[1]))

    def test_repair_fills_contribute_cost_without_inventing_a_repair_slippage_quote(self):
        row = batch()
        row["legs"][1]["receipt"].update(status="REJECTED", executedQty="0", avgPrice="0", cumQuote="0")
        repair = deepcopy(row["legs"][0])
        repair["order"].update(side="SELL", newClientOrderId="repair")
        repair["receipt"].update(side="SELL", clientOrderId="repair", avgPrice="99.5", cumQuote="199")
        row["repairs"].append(repair)
        self.save(row)
        result = self.report()["daily"]
        self.assertEqual((result["estimated_fee"], result["spread_cost"], result["total_cost"]), ("0.05", "2", "2.05"))
        self.assertTrue(result["complete"])
        self.assertFalse(result["slippage_complete"])
        self.assertIsNone(result["slippage_cost"])

    def test_partial_and_unknown_receipts_keep_known_subtotals_and_unmatched_notional(self):
        row = batch()
        row["legs"][1]["receipt"].update(status="PARTIALLY_FILLED", executedQty="1", cumQuote="99.5")
        row.pop("finished_at")
        result = self.report(pending=row)["daily"]
        self.assertEqual(result["estimated_fee"], "0.0375625")
        self.assertEqual(result["spread_cost"], "1")
        self.assertEqual(result["unmatched_notional"], "100.5")
        self.assertFalse(result["complete"])
        self.assertEqual(result["missing_count"], 1)
        row["legs"][1]["receipt"] = None
        result = self.report(pending=row)["daily"]
        self.assertEqual(result["estimated_fee"], "0.025125")
        self.assertEqual(result["unmatched_notional"], "201")
        self.assertEqual(result["fill_count"], 1)
        self.assertFalse(result["complete"])

    def test_finalized_record_takes_precedence_over_duplicate_pending(self):
        row = batch()
        self.save(row)
        pending = deepcopy(row)
        pending["legs"][0]["receipt"]["cumQuote"] = "999"
        self.assertEqual(self.report(pending=pending)["daily"]["total_cost"], "2.05")

    def test_only_cycle_batches_and_matching_group_identity_contribute(self):
        ordinary = batch("ordinary")
        ordinary["kind"] = "ordinary"
        self.save(ordinary)
        other = batch("old-group")
        other["pair_id"] = other["execution_quality"]["pair_id"] = "deleted-group"
        self.save(other)
        self.store.put("pair_deleted:deleted-group", {"deleted_at": NOW - 5})
        self.assertEqual(self.report()["daily"]["fill_count"], 0)
        self.assertTrue(self.report()["daily"]["complete"])
        mismatched = batch("bad-identity")
        mismatched["identities"]["short"]["account_id"] = "unrelated"
        self.save(mismatched)
        result = self.report()["daily"]
        self.assertEqual(result["estimated_fee"], "0")
        self.assertEqual(result["unassigned_count"], 1)
        self.assertFalse(result["complete"])

    def test_legacy_quality_binds_owner_but_account_tuple_alone_is_only_a_gap(self):
        row = batch()
        row.pop("pair_id")
        self.save(row)
        self.assertEqual(self.report()["daily"]["total_cost"], "2.05")
        row.pop("execution_quality")
        self.save(row)
        result = self.report()["daily"]
        self.assertEqual(result["estimated_fee"], "0")
        self.assertEqual(result["unassigned_count"], 1)
        new_pair = {**PAIR, "id": "new-group"}
        result = self.report(pair=new_pair)["daily"]
        self.assertEqual(result["total_cost"], "0")
        self.assertFalse(result["complete"])

    def test_current_runtime_ownership_can_bind_pending_without_quality(self):
        row = batch()
        row.pop("pair_id")
        row.pop("execution_quality")
        result = self.report(pending=row)["daily"]
        self.assertEqual(result["total_cost"], "2.05")
        self.assertTrue(result["complete"])
        self.assertIsNone(result["slippage_cost"])

    def test_cross_midnight_receipts_are_unassigned_daily_but_known_within_week(self):
        midnight = datetime(2026, 9, 30, tzinfo=timezone.utc).timestamp()
        row = batch(created=midnight - 1)
        self.save(row)
        result = self.report()
        self.assertEqual(result["daily"]["unassigned_count"], 2)
        self.assertEqual(result["daily"]["total_cost"], "0")
        self.assertFalse(result["daily"]["complete"])
        self.assertEqual(result["weekly"]["total_cost"], "2.05")
        self.assertTrue(result["weekly"]["complete"])

    def test_monday_week_boundary_does_not_attribute_crossing_receipts_to_one_week(self):
        monday = datetime(2026, 9, 28, tzinfo=timezone.utc).timestamp()
        self.save(batch(created=monday - 1))
        result = self.report(now=monday + 10)
        self.assertEqual(result["weekly"]["start"], monday)
        self.assertEqual(result["weekly"]["end"], monday + 7 * 86400)
        self.assertEqual(result["weekly"]["unassigned_count"], 2)
        self.assertFalse(result["weekly"]["complete"])

    def test_missing_timestamps_do_not_become_zero_or_finished_at_execution_dates(self):
        row = batch()
        for leg in row["legs"]:
            leg["receipt"].pop("updateTime")
        self.save(row)
        result = self.report()["daily"]
        self.assertEqual(result["fill_count"], 0)
        self.assertEqual(result["unassigned_count"], 2)
        self.assertFalse(result["complete"])

    def test_missing_and_partial_slippage_preserves_available_subtotal(self):
        self.save(batch("known"))
        unknown = batch("missing")
        unknown.pop("execution_quality")
        self.save(unknown)
        result = self.report()["daily"]
        self.assertEqual(result["slippage_cost"], "1")
        self.assertFalse(result["slippage_complete"])
        self.assertTrue(result["complete"])
        self.assertEqual(result["total_cost"], "4.1")

    def test_foreign_quality_reference_cannot_authorize_a_slippage_comparison(self):
        row = batch()
        row["execution_quality"]["phase"] = "close"
        self.save(row)
        result = self.report()["daily"]
        self.assertEqual(result["total_cost"], "2.05")
        self.assertIsNone(result["slippage_cost"])
        self.assertFalse(result["slippage_complete"])

    def test_late_receipt_correction_is_not_hidden_by_old_finished_at(self):
        row = batch(created=NOW - 30 * 86400)
        for leg in row["legs"]:
            leg["receipt"]["updateTime"] = int((NOW - 1) * 1000)
        self.save(row)
        result = self.report()["daily"]
        self.assertEqual(result["unassigned_count"], 2)
        self.assertFalse(result["complete"])

    def test_negative_spread_is_a_gain_not_clamped_or_added_again_as_slippage(self):
        self.save(batch(buy="99.5", sell="100.5"))
        result = self.report()["daily"]
        self.assertEqual(result["spread_cost"], "-2")
        self.assertEqual(result["total_cost"], "-1.95")

    def test_known_zero_fills_need_no_execution_timestamp(self):
        row = batch()
        for leg in row["legs"]:
            leg["receipt"].update(status="REJECTED", executedQty="0", avgPrice="0", cumQuote="0", local_not_sent=True)
            leg["receipt"].pop("updateTime")
        self.save(row)
        result = self.report()["daily"]
        self.assertTrue(result["complete"])
        self.assertEqual(result["total_cost"], "0")
        self.assertEqual(result["fill_count"], 0)

    def test_duplicate_client_id_is_not_double_counted(self):
        row = batch()
        row["repairs"].append(deepcopy(row["legs"][0]))
        self.save(row)
        result = self.report()["daily"]
        self.assertEqual(result["estimated_fee"], "0.05")
        self.assertEqual(result["fill_count"], 2)
        self.assertEqual(result["missing_count"], 1)
        self.assertFalse(result["complete"])

    def test_repeated_polls_skip_history_and_each_commit_normalizes_only_changed_batch(self):
        with self.store.connect() as db:
            db.executemany("INSERT INTO kv VALUES (?,?)", [("pair_batch:old-" + str(i), dumps(batch("old-" + str(i), created=NOW - 90 * 86400)))
                                                          for i in range(100)])
        self.save(batch())
        with patch("trading.pair_cost._contribution", wraps=pair_cost._contribution) as normalize:
            self.assertEqual(self.report()["daily"]["total_cost"], "2.05")
            self.assertEqual(normalize.call_count, 101)
            for i in range(1, 6):
                self.report(now=NOW + i)
            self.assertEqual(normalize.call_count, 101)
            self.save(batch("new"))
            self.assertEqual(self.report(now=NOW + 6)["daily"]["total_cost"], "4.1")
            self.assertEqual(normalize.call_count, 102)

    def test_current_week_includes_more_than_one_hundred_complete_batches(self):
        with self.store.connect() as db:
            db.executemany("INSERT INTO kv VALUES (?,?)", [("pair_batch:week-" + str(i), dumps(batch("week-" + str(i))))
                                                          for i in range(101)])
        result = self.report()["weekly"]
        self.assertEqual(result["estimated_fee"], "5.05")
        self.assertEqual(result["total_cost"], "207.05")
        self.assertEqual(result["fill_count"], 202)
        self.assertTrue(result["complete"])

    def test_another_store_update_and_delete_revise_cached_totals(self):
        self.save(batch())
        self.assertEqual(self.report()["daily"]["total_cost"], "2.05")
        other = Store(self.store.path)
        changed = batch(buy="101.5")
        other.put("pair_batch:batch", changed)
        self.assertEqual(self.report()["daily"]["total_cost"], "4.05025")
        with other.connect() as db:
            db.execute("DELETE FROM kv WHERE key='pair_batch:batch'")
        self.assertEqual(self.report()["daily"]["total_cost"], "0")

    def test_rollback_does_not_publish_a_revision_or_replacement_amount(self):
        self.save(batch())
        before = self.report()
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store.connect() as db:
                db.execute("UPDATE kv SET data=? WHERE key='pair_batch:batch'", (dumps(batch(buy="500")),))
                raise RuntimeError("rollback")
        with patch("trading.pair_cost._contribution", side_effect=AssertionError("valid cache")):
            self.assertEqual(self.report(), before)

    def test_old_wal_snapshot_cannot_read_or_poison_newer_cached_amounts(self):
        self.save(batch())
        with self.store.read_snapshot() as old:
            old.get("pair_batch:batch")  # Establish the reader's snapshot.
            Store(self.store.path).put("pair_batch:batch", batch(buy="101.5"))
            self.assertEqual(self.report()["daily"]["total_cost"], "4.05025")
            self.assertEqual(self.report(reader=old)["daily"]["total_cost"], "2.05")
        self.assertEqual(self.report()["daily"]["total_cost"], "4.05025")

    def test_utc_rollover_and_future_receipt_release_refresh_without_database_writes(self):
        self.save(batch())
        self.assertEqual(self.report()["daily"]["total_cost"], "2.05")
        tomorrow = datetime(2026, 10, 1, tzinfo=timezone.utc).timestamp()
        result = self.report(now=tomorrow)
        self.assertEqual(result["daily"]["total_cost"], "0")
        self.assertEqual(result["weekly"]["total_cost"], "2.05")
        self.save(batch("future", created=tomorrow + 10))
        self.assertEqual(self.report(now=tomorrow)["daily"]["fill_count"], 0)
        self.assertEqual(self.report(now=tomorrow + 13)["daily"]["fill_count"], 2)

    def test_clock_rollback_after_incremental_update_excludes_newer_fills(self):
        self.save(batch())
        self.assertEqual(self.report()["daily"]["total_cost"], "2.05")
        self.save(batch("later", created=NOW + 10))
        self.assertEqual(self.report(now=NOW + 20)["daily"]["total_cost"], "4.1")
        self.assertEqual(self.report(now=NOW + 5)["daily"]["total_cost"], "2.05")
        self.assertEqual(self.report(now=NOW + 21)["daily"]["total_cost"], "4.1")

    def test_caller_mutation_cannot_change_shared_cache(self):
        self.save(batch())
        result = self.report()
        result["daily"]["total_cost"] = "999"
        self.assertEqual(self.report()["daily"]["total_cost"], "2.05")


class PairCostIntegrationTests(TestCase):
    def setUp(self):
        from tests.test_pair_trading import PairTradingTests
        self.fixture = PairTradingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_real_paper_open_close_costs_survive_restart_without_exchange_reads(self):
        fixture = self.fixture
        opened = fixture.tick()
        fixture.expire()
        closed = fixture.tick()
        self.assertEqual(closed["progress"]["completed_cycles"], 1)
        self.assertEqual(fixture.store.get("pair_batch:" + opened["last_batch"]["id"])["pair_id"], "gold")
        with Store(fixture.store.path).read_snapshot() as reader:
            with patch.object(fixture.brokers["long"], "snapshot", side_effect=AssertionError("read-only report")):
                result = fixture.engine.pairs.states(reader)[0]["state"]["cycle_costs"]
        self.assertEqual(result["daily"]["fill_count"], 4)
        self.assertTrue(result["daily"]["complete"])
        self.assertGreater(Fraction(result["daily"]["estimated_fee"]), 0)

    def test_reporting_failure_is_null_and_does_not_interrupt_pair_execution(self):
        fixture = self.fixture
        with patch("trading.pair_cost.read", side_effect=RuntimeError("report unavailable")):
            self.assertEqual(fixture.tick()["phase"], "holding")
            with fixture.store.read_snapshot() as reader:
                result = fixture.engine.pairs.states(reader)[0]
        self.assertIsNone(result["state"]["cycle_costs"])
        self.assertEqual(result["state"]["phase"], "holding")
