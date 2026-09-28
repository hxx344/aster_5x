"""Read-only dashboard projection must retain the durable transfer boundary."""
from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from trading.margin_balance import DEFAULT_MARGIN, MarginBalancer


class MarginStatusTests(TestCase):
    def setUp(self):
        self.pair = {"enabled": True, "margin": {**DEFAULT_MARGIN, "enabled": True}}
        self.journal = {"checked_at": 90, "next_check_at": 95}

    def view(self, journal=None, runtime=None):
        with patch("trading.margin_balance.time.time", return_value=100):
            return MarginBalancer.status_view(self.pair, self.journal if journal is None else journal, runtime)

    def test_new_pending_overrides_old_runtime_and_filters_private_fields(self):
        self.journal["pending"] = {"request_id": "new", "status": "submitting", "source": "long", "destination": "short",
            "amount": "10", "created_at": 99, "identity": "private-binding", "before_receipts": {"long": ["private-id"]}}
        before = deepcopy(self.journal)
        runtime = {"enabled": True, "status": "waiting", "reason": "old", "checked_at": 90, "pending": None}
        view = self.view(runtime=runtime)
        self.assertEqual(view["status"], "unknown")
        self.assertTrue(view["blocks_trading"])
        self.assertEqual(view["pending"]["request_id"], "new")
        self.assertNotIn("private", str(view))
        self.assertEqual(self.journal, before)

    def test_pending_survives_disabled_paused_configuration(self):
        self.pair["enabled"] = False
        self.pair["margin"]["enabled"] = False
        self.journal["pending"] = {"status": "acknowledged", "request_id": "pending"}
        view = self.view()
        self.assertFalse(view["enabled"])
        self.assertEqual(view["status"], "acknowledged")
        self.assertTrue(view["blocks_trading"])

    def test_durable_failure_overrides_runtime_waiting(self):
        self.journal["blocked_reason"] = "账户资格检查失败"
        view = self.view(runtime={"status": "waiting", "reason": "old"})
        self.assertEqual(view["status"], "blocked")
        self.assertEqual(view["reason"], self.journal["blocked_reason"])
        self.assertTrue(view["blocks_trading"])

    def test_disabled_or_paused_clears_nonpending_failure_only(self):
        self.journal["blocked_reason"] = "上次账户资格检查失败"
        self.pair["enabled"] = False
        self.assertEqual(self.view()["status"], "paused")
        self.assertFalse(self.view()["blocks_trading"])
        self.pair["margin"]["enabled"] = False
        self.assertEqual(self.view()["status"], "disabled")
        self.assertFalse(self.view()["blocks_trading"])
        self.journal["pending"] = {"status": "unknown", "request_id": "pending"}
        self.assertEqual(self.view()["status"], "unknown")
        self.assertTrue(self.view()["blocks_trading"])

    def test_disabled_paused_and_cooldown_are_derived_from_current_state(self):
        self.journal["cooldown_until"] = 120
        self.assertEqual(self.view()["status"], "cooldown")
        self.pair["enabled"] = False
        self.assertEqual(self.view()["status"], "paused")
        self.pair["margin"]["enabled"] = False
        self.assertEqual(self.view()["status"], "disabled")

    def test_current_execution_reason_is_retained_without_raw_journal_fields(self):
        self.journal["last_transfer"] = {"request_id": "old", "status": "confirmed", "identity": "private"}
        runtime = {"enabled": True, "checked_at": 90, "pending": None, "status": "waiting", "reason": "余额差未达到阈值",
            "last_transfer": {"request_id": "old", "status": "confirmed"}, "signature": "private"}
        view = self.view(runtime=runtime)
        self.assertEqual(view["reason"], runtime["reason"])
        self.assertEqual(view["last_transfer"], runtime["last_transfer"])
        self.assertNotIn("private", str(view))

    def test_old_runtime_cannot_restore_resolved_pending(self):
        runtime = {"enabled": True, "checked_at": 90, "status": "unknown", "reason": "old",
            "pending": {"request_id": "old", "status": "unknown"}, "blocks_trading": True}
        view = self.view(runtime=runtime)
        self.assertEqual(view["status"], "waiting")
        self.assertIsNone(view["pending"])
        self.assertFalse(view["blocks_trading"])

    def test_old_runtime_cannot_override_new_journal_check(self):
        runtime = {"enabled": True, "checked_at": 89, "pending": None, "last_transfer": None,
            "status": "blocked", "reason": "old", "blocks_trading": True}
        self.assertEqual(self.view(runtime=runtime)["status"], "waiting")

    def test_malformed_journal_and_timing_remain_blocked(self):
        for journal in ([], {"pending": {}}, {"cooldown_until": None}, {"next_check_at": float("nan")}):
            with self.subTest(journal=journal):
                view = self.view(journal)
                self.assertEqual(view["status"], "blocked")
                self.assertTrue(view["blocks_trading"])
