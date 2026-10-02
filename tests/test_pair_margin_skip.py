"""Atomic abandonment of an unknown transfer after a new account baseline."""
from copy import deepcopy
import time
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_margin as fixtures
from trading.exchange import AmbiguousOrder, ExchangeError
from trading.margin_balance import MarginBalancer


class PairMarginSkipTests(TestCase):
    setUp = fixtures.PairMarginTests.setUp
    live = fixtures.PairMarginTests.live
    state = fixtures.PairMarginTests.state
    ready = fixtures.PairMarginTests.ready
    snapshots = fixtures.PairMarginTests.snapshots

    def unknown(self):
        snapshots = self.live()
        for member in self.members.values():
            member["enabled"] = False
            self.store.save_account(member)
        self.pair = self.store.save_pair(self.pair, create=True)
        self.response = AmbiguousOrder("uncertain write")
        result = self.balancer.tick(self.pair, snapshots)
        self.assertEqual(result["status"], "unknown", result)
        self.assertEqual(len(self.transfers), 1)
        self.ready()
        self.original = deepcopy(self.state()["pending"])
        self.archive_key = "pair_margin_skipped:gold:" + self.original["request_id"]

    def assert_preserved(self, result):
        self.assertTrue(result["blocks_trading"], result)
        self.assertEqual(self.state()["pending"], self.original)
        self.assertIsNone(self.store.get(self.archive_key))
        self.assertEqual(len(self.transfers), 1)

    def test_archive_and_clear_are_atomic_and_restarted_risk_uses_actual_balance(self):
        self.unknown()
        result = self.balancer.tick(self.pair, {})
        self.assertEqual(result["status"], "skipped", result)
        self.assertTrue(result["blocks_trading"])
        self.assertIsNone(self.state()["pending"])
        archive = self.store.get(self.archive_key)
        self.assertEqual(archive["original"], self.original)
        self.assertEqual(archive["result"], self.state()["last_transfer"])
        self.assertNotIn("confirmed_at", archive["result"])
        self.assertEqual(archive["baseline"]["long"]["available"], "3000")
        self.assertGreaterEqual(archive["result"]["skipped_at"], self.original["created_at"])
        restarted = MarginBalancer(self.engine)
        raw = self.snapshots()
        self.assertIs(restarted.risk_snapshots(self.pair, raw), raw)
        self.assertFalse(restarted.tick(self.pair, {})["blocks_trading"])
        view = restarted.status_view(self.pair, self.state(), result)
        self.assertIsNone(view["pending"])
        self.assertFalse(view["blocks_trading"])
        self.assertEqual(view["last_transfer"]["status"], "skipped")
        self.assertEqual(len(self.transfers), 1)

    def test_old_ready_marker_and_transaction_id_still_require_two_new_reads(self):
        self.unknown()
        state = self.state()
        state["pending"].update(trading_baseline_at=time.time(), transaction_id="old-transaction")
        self.store.put("pair_margin:gold", state)
        self.refreshed.clear()
        with patch.object(self.balancer, "_income", side_effect=AssertionError("skip must not poll old income")):
            result = self.balancer.tick(self.pair, {})
        self.assertEqual(result["status"], "skipped", result)
        self.assertEqual(self.refreshed, ["long", "short"])
        self.assertEqual(self.store.get(self.archive_key)["original"]["transaction_id"], "old-transaction")

    def test_second_account_read_failure_preserves_pending_without_archive(self):
        self.unknown()
        with patch.object(self.brokers["short"], "margin_snapshot", side_effect=ExchangeError("offline")):
            self.assert_preserved(self.balancer.tick(self.pair, {}))

    def test_snapshot_from_before_attempt_cannot_clear_pending(self):
        self.unknown()
        old = self.snapshots()["long"]
        with patch.object(self.brokers["long"], "margin_snapshot", return_value=old):
            self.assert_preserved(self.balancer.tick(self.pair, {}))

    def test_journal_write_failure_rolls_back_archive_insert(self):
        self.unknown()
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER fail_skip BEFORE UPDATE ON kv "
                       "WHEN NEW.key='pair_margin:gold' AND json_extract(NEW.data,'$.pending') IS NULL "
                       "BEGIN SELECT RAISE(ABORT, 'test disk failure'); END")
        self.assert_preserved(self.balancer.tick(self.pair, {}))

    def test_pair_change_during_read_preserves_pending(self):
        self.unknown()
        fresh = self.brokers["short"].margin_snapshot

        def changed(*args, **kwargs):
            result = fresh(*args, **kwargs)
            self.store.save_pair({**self.pair, "name": "changed while reading"})
            return result

        with patch.object(self.brokers["short"], "margin_snapshot", side_effect=changed):
            self.assert_preserved(self.balancer.tick(self.pair, {}))

    def test_account_change_during_read_preserves_pending(self):
        self.unknown()
        fresh = self.brokers["short"].margin_snapshot

        def changed(*args, **kwargs):
            result = fresh(*args, **kwargs)
            self.store.save_account({**self.members["long"], "name": "changed while reading"})
            return result

        with patch.object(self.brokers["short"], "margin_snapshot", side_effect=changed):
            self.assert_preserved(self.balancer.tick(self.pair, {}))

    def test_new_balance_request_waits_for_cooldown_and_uses_a_new_request_id(self):
        self.unknown()
        result = self.balancer.tick(self.pair, {})
        self.assertEqual(result["status"], "skipped", result)
        self.assertEqual(self.balancer.tick(self.pair, {})["status"], "cooldown")
        self.assertEqual(len(self.transfers), 1)
        self.response = {"code": 200, "msg": "success"}
        deadline = max(self.state()["cooldown_until"], self.state()["next_check_at"])
        with patch("time.time", return_value=deadline + 1):
            result = self.balancer.tick(self.pair, self.snapshots())
        self.assertEqual(result["status"], "acknowledged", result)
        self.assertEqual(len(self.transfers), 2)
        self.assertNotEqual(self.state()["last_transfer"]["request_id"], self.original["request_id"])
        self.assertIsNotNone(self.store.get(self.archive_key))

    def test_journal_change_during_read_cannot_be_overwritten_by_skip(self):
        self.unknown()
        fresh = self.brokers["short"].margin_snapshot

        def changed(*args, **kwargs):
            result = fresh(*args, **kwargs)
            state = self.state()
            state["pending"]["transaction_id"] = "late-receipt"
            self.store.put("pair_margin:gold", state)
            return result

        with patch.object(self.brokers["short"], "margin_snapshot", side_effect=changed):
            result = self.balancer.tick(self.pair, {})
        self.assertTrue(result["blocks_trading"])
        self.assertEqual(self.state()["pending"]["transaction_id"], "late-receipt")
        self.assertIsNone(self.store.get(self.archive_key))
        self.assertEqual(len(self.transfers), 1)
