"""A paired snapshot can reuse current position evidence, never another account's mode."""
from copy import deepcopy
import time
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_budget as fixtures
from trading.exchange import SnapshotSuperseded
from trading.models import AccountModeError, TradingError

SYMBOL = fixtures.SYMBOL
ACCOUNT = "/fapi/v3/accountWithJoinMargin"
RISK = "/fapi/v3/positionRisk"


class PairAccountModeReuseTests(TestCase):
    setUp = fixtures.PairBudgetTests.setUp
    live_brokers = fixtures.PairBudgetTests.live_brokers

    def prepare(self):
        self.live_brokers()
        broker = self.brokers["long"]
        broker.snapshot([SYMBOL], reuse_account_mode=True)
        return broker

    @staticmethod
    def count(broker, path):
        return sum(row[1] == path for row in broker.api.calls)

    def test_current_rows_replace_only_periodic_dual_get_without_renewing_its_cache(self):
        wall, ticks = time.time(), time.monotonic()
        with patch("time.time", side_effect=lambda: wall), patch("time.monotonic", side_effect=lambda: ticks):
            broker = self.prepare()
            stamp = broker.cached_at["dual"]
            wall += 20
            ticks += 20
            snapshot = broker.snapshot([SYMBOL], reuse_account_mode=True)
            snapshot.require_modes([SYMBOL])
            broker.require_snapshot_current(snapshot)
            self.assertEqual(self.count(broker, fixtures.DUAL), 1)
            self.assertEqual(self.count(broker, fixtures.MULTI), 2)
            self.assertEqual(self.count(broker, ACCOUNT), 2)
            self.assertEqual(self.count(broker, RISK), 2)
            self.assertEqual(broker.cached_at["dual"], stamp)
            self.assertNotEqual(snapshot.brackets, {})

    def test_startup_recovery_and_mode_events_require_independent_gets(self):
        broker = self.prepare()
        broker.snapshot([SYMBOL], fresh_modes=True, reuse_account_mode=True)
        self.assertEqual(self.count(broker, fixtures.DUAL), 2)
        snapshot = broker.snapshot([SYMBOL], reuse_account_mode=True)
        broker._cycle_account_event("ACCOUNT_CONFIG_UPDATE")
        with self.assertRaises(TradingError):
            broker.require_snapshot_current(snapshot)
        broker.api.hedge = False
        blocked = broker.snapshot([SYMBOL], reuse_account_mode=True)
        self.assertEqual(self.count(broker, fixtures.DUAL), 3)
        with self.assertRaises(AccountModeError):
            blocked.require_modes([SYMBOL])
        self.assertFalse(broker.snapshot([SYMBOL], reuse_account_mode=True).hedge_mode)

    def test_each_member_requires_its_own_initial_mode_and_authentication(self):
        long = self.prepare()
        short = self.brokers["short"]
        short.api.hedge = False
        self.assertFalse(short.snapshot([SYMBOL], reuse_account_mode=True).hedge_mode)
        self.assertTrue(long.snapshot([SYMBOL], reuse_account_mode=True).hedge_mode)
        self.assertEqual(self.count(short, fixtures.DUAL), 1)
        self.assertEqual(self.count(long, fixtures.DUAL), 1)

    def test_incomplete_duplicate_or_oneway_rows_cannot_prove_a_paired_mode(self):
        broker = self.prepare()
        original = broker.api.call
        cases = [(ACCOUNT, "missing"), (ACCOUNT, "duplicate"), (ACCOUNT, "both"), (RISK, "both")]
        for endpoint, change in cases:
            with self.subTest(endpoint=endpoint, change=change):
                def mutate(method, path, *args, **kwargs):
                    payload = original(method, path, *args, **kwargs)
                    if path == endpoint:
                        rows = payload["positions"] if path == ACCOUNT else payload
                        if change == "missing":
                            rows[:] = [row for row in rows if (row["symbol"], row["positionSide"]) != (SYMBOL, "SHORT")]
                        elif change == "duplicate":
                            rows.append(deepcopy(rows[0]))
                        else:
                            rows.append({**rows[0], "symbol": "OUTSIDEUSD1", "positionSide": "BOTH", "positionAmt": "0"})
                    return payload
                with patch.object(broker.api, "call", side_effect=mutate), self.assertRaises(TradingError):
                    broker.snapshot([SYMBOL], reuse_account_mode=True)
        self.assertTrue(all(method == "GET" for method, _, _ in broker.api.calls))

    def test_current_account_and_risk_still_must_agree(self):
        broker = self.prepare()
        original = broker.api.call
        for field, value in (("leverage", "10"), ("marginType", "isolated"), ("positionAmt", "1")):
            with self.subTest(field=field):
                def mutate(method, path, *args, **kwargs):
                    payload = original(method, path, *args, **kwargs)
                    if path == RISK:
                        row = next(row for row in payload if row["symbol"] == SYMBOL)
                        row[field] = value
                        if field == "positionAmt":
                            row["entryPrice"] = "4412"
                    return payload
                with patch.object(broker.api, "call", side_effect=mutate), self.assertRaises(TradingError):
                    broker.snapshot([SYMBOL], reuse_account_mode=True)

    def test_event_during_read_or_after_publication_revokes_snapshot(self):
        broker = self.prepare()
        original = broker.api.call
        def changed(method, path, *args, **kwargs):
            payload = original(method, path, *args, **kwargs)
            if path == ACCOUNT:
                broker._cycle_account_event("ACCOUNT_UPDATE")
            return payload
        with patch.object(broker.api, "call", side_effect=changed), self.assertRaises(SnapshotSuperseded):
            broker.snapshot([SYMBOL], reuse_account_mode=True)
        snapshot = broker.snapshot([SYMBOL], reuse_account_mode=True)
        broker._cycle_account_event("ORDER_TRADE_UPDATE")
        with self.assertRaises(TradingError):
            broker.require_snapshot_current(snapshot)

    def test_unpaired_snapshot_retains_original_mode_refresh(self):
        wall, ticks = time.time(), time.monotonic()
        with patch("time.time", side_effect=lambda: wall), patch("time.monotonic", side_effect=lambda: ticks):
            broker = self.prepare()
            wall += 20
            ticks += 20
            broker.snapshot([SYMBOL])
            self.assertEqual(self.count(broker, fixtures.DUAL), 2)
