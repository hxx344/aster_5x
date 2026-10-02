"""Skip unknown writes after fresh balances; protect legacy pending windows."""
from copy import deepcopy
import time
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_margin as fixtures
from trading.exchange import AmbiguousOrder, ExchangeError
from trading.margin_balance import MarginBalancer
from trading.models import TradingError, dec


class PairMarginResumeTests(TestCase):
    setUp = fixtures.PairMarginTests.setUp
    live = fixtures.PairMarginTests.live
    state = fixtures.PairMarginTests.state
    ready = fixtures.PairMarginTests.ready
    snapshots = fixtures.PairMarginTests.snapshots

    def unknown(self):
        snapshots = self.live()
        self.response = AmbiguousOrder("uncertain write")
        result = self.balancer.tick(self.pair, snapshots)
        self.assertTrue(result["blocks_trading"])
        self.assertEqual(len(self.transfers), 1)
        self.ready()
        return deepcopy(self.state()["pending"])

    def resume(self):
        result = self.balancer.tick(self.pair, {})
        self.assertTrue(result["blocks_trading"], result)
        self.assertEqual(result["status"], "skipped")
        self.assertIsNone(result["pending"])
        self.assertEqual(result["last_transfer"]["status"], "skipped")
        self.assertEqual(len(self.transfers), 1)
        return result

    def legacy_reservation(self):
        original = self.unknown()
        state = self.state()
        state["pending"]["trading_baseline_at"] = time.time()
        state["next_check_at"] = time.time() + 30
        self.store.put("pair_margin:gold", state)
        return original

    def test_read_failure_without_write_skips_then_retries_fresh_after_deadline(self):
        original = ExchangeError("sensitive upstream text", http_status=503)
        with patch("time.time", return_value=1000), patch.object(self.balancer, "_live") as submit:
            result = self.balancer.tick(self.pair, {}, snapshot_loader=lambda: (_ for _ in ()).throw(original))
            self.assertFalse(result["blocks_trading"])
            self.assertIn("503", result["reason"])
            self.assertNotIn("sensitive upstream text", result["reason"])
            submit.assert_not_called()
        with patch("time.time", return_value=1001):
            with patch.object(self.balancer, "_check_snapshots", side_effect=AssertionError("not due")):
                self.assertFalse(self.balancer.tick(self.pair, {})["blocks_trading"])
        with patch("time.time", return_value=1006):
            # The new read, not the failed/old snapshot, becomes transfer authority.
            result = self.balancer.tick(self.pair, {}, snapshot_loader=lambda: (self.snapshots(), {}))
        self.assertEqual(result["status"], "paper_confirmed")
        self.assertNotIn("blocked_reason", self.state())

    def test_legacy_pending_window_reserves_full_debit_on_copy_without_changing_display(self):
        original = self.legacy_reservation()
        raw = self.snapshots()
        reserved = self.balancer.risk_snapshots(self.pair, raw)
        for field in ("wallet", "available", "equity"):
            self.assertEqual(getattr(reserved["long"], field), getattr(raw["long"], field) - dec(original["amount"]))
            self.assertEqual(getattr(raw["long"], field), 3000)
        self.assertIs(reserved["short"], raw["short"])
        self.assertEqual(reserved["long"].account_read_generation, raw["long"].account_read_generation)
        self.brokers["long"].require_snapshot_current(reserved["long"])
        stored = deepcopy(self.state()["pending"])
        self.assertIsNotNone(stored.pop("trading_baseline_at"))
        self.assertEqual(stored, original)

    def test_failed_refresh_stays_blocked_until_both_new_reads_succeed(self):
        self.unknown()
        self.refresh_error = ExchangeError("offline")
        self.assertTrue(self.balancer.tick(self.pair, {})["blocks_trading"])
        self.assertNotIn("trading_baseline_at", self.state()["pending"])
        self.refresh_error = None
        self.ready()
        self.resume()

    def test_restored_identity_clears_old_hard_failure(self):
        self.unknown()
        user = self.creds["ASTER_LONG"]["user"]
        self.creds["ASTER_LONG"]["user"] = "0x" + "a" * 40
        self.ready()
        self.assertTrue(self.balancer.tick(self.pair, {})["blocks_trading"])
        self.creds["ASTER_LONG"]["user"] = user
        self.ready()
        self.resume()
        self.assertNotIn("blocked_reason", self.state())

    def test_restart_and_disabled_balancer_keep_skipped_record_without_reservation_or_resend(self):
        self.unknown()
        self.resume()
        self.pair["enabled"] = False
        self.pair["margin"]["enabled"] = False
        restarted = MarginBalancer(self.engine)
        self.ready()
        self.assertFalse(restarted.tick(self.pair, {})["blocks_trading"])
        raw = self.snapshots()
        self.assertIs(restarted.risk_snapshots(self.pair, raw), raw)
        self.assertEqual(raw["long"].available, 3000)
        self.assertIsNone(self.state()["pending"])
        self.assertEqual(self.state()["last_transfer"]["status"], "skipped")
        self.assertEqual(len(self.transfers), 1)

    def test_legacy_pending_old_or_revoked_reads_cannot_authorize_reserved_trading(self):
        self.legacy_reservation()
        raw = self.snapshots()
        before = self.state()["pending"]["trading_baseline_at"] - 0.01
        raw["long"].timestamp = before
        with self.assertRaises(TradingError):
            self.balancer.risk_snapshots(self.pair, raw)
        raw = self.snapshots()
        reserved = self.balancer.risk_snapshots(self.pair, raw)
        self.brokers["long"]._cycle_account_event("ACCOUNT_UPDATE")
        with self.assertRaises(TradingError):
            self.brokers["long"].require_snapshot_current(reserved["long"])

    def test_invalid_baseline_and_changed_identity_cannot_unlock_risk_check(self):
        self.legacy_reservation()
        saved = self.state()
        for baseline in (None, True, float("nan"), time.time() + 100, saved["pending"]["created_at"] - 1):
            changed = deepcopy(saved)
            changed["pending"]["trading_baseline_at"] = baseline
            self.store.put("pair_margin:gold", changed)
            with self.assertRaises(TradingError):
                self.balancer.risk_snapshots(self.pair, self.snapshots())
        self.store.put("pair_margin:gold", saved)
        self.creds["ASTER_LONG"]["user"] = "0x" + "a" * 40
        with self.assertRaises(TradingError):
            self.balancer.risk_snapshots(self.pair, self.snapshots())

    def test_recovery_reserves_even_before_baseline_to_allow_only_safe_reduction(self):
        original = self.unknown()
        raw = self.snapshots()
        with self.assertRaises(TradingError):
            self.balancer.risk_snapshots(self.pair, raw)
        self.assertEqual(self.balancer.risk_snapshots(self.pair, raw, recovery=True)["long"].equity,
                         raw["long"].equity - dec(original["amount"]))

    def test_unknown_with_transaction_id_is_skipped_without_income_queries(self):
        self.unknown()
        state = self.state()
        state["pending"]["transaction_id"] = "known-transfer"
        self.store.put("pair_margin:gold", state)
        with patch.object(self.balancer, "_income", side_effect=ExchangeError("income unavailable", retry_after=60)) as income:
            self.resume()
            income.assert_not_called()
            self.ready()
            self.assertEqual(self.balancer.tick(self.pair, {})["status"], "cooldown")
            income.assert_not_called()
            self.assertIsNone(self.state()["pending"])
            self.assertEqual(self.state()["last_transfer"]["status"], "skipped")
            self.assertNotIn("confirmed_at", self.state()["last_transfer"])
            self.assertFalse(MarginBalancer.status_view(self.pair, self.state())["blocks_trading"])
