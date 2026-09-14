from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import threading
import unittest
from unittest.mock import Mock, patch

from trading.account_cache import CycleAccountCache, HotAccountUnavailable
from trading.models import AccountSnapshot


NOW = 1_800_000_000
SYMBOLS = ("XAUUSD1", "CLUSD1")


def snapshot(**changes):
    values = dict(equity=Decimal("100"), maintenance=Decimal("1"), available=Decimal("99"),
        wallet=Decimal("100"), unrealized=Decimal("0"), positions=[], open_orders=None,
        hedge_mode=True, multi_assets=False, can_trade=True, timestamp=NOW)
    values.update(changes)
    return AccountSnapshot(**values)


def ready_cache(**kwargs):
    wall, ticks = Mock(return_value=NOW), Mock(return_value=100)
    cache = CycleAccountCache(clock=wall, monotonic=ticks, **kwargs)
    cache.configure(SYMBOLS)
    cache.set_connected(True)
    ticket = cache.begin_refresh()
    if not cache.publish(ticket, snapshot(), 100):
        raise AssertionError("Test cache initialization failed")
    return cache, wall, ticks


class CycleAccountCacheTests(unittest.TestCase):
    def test_initial_configuration_and_private_connection_are_required(self):
        cache = CycleAccountCache()
        with self.assertRaises(HotAccountUnavailable):
            cache.begin_refresh()
        cache.configure(SYMBOLS)
        with self.assertRaises(HotAccountUnavailable):
            cache.begin_refresh()
        cache.set_connected(True)
        self.assertTrue(cache.begin_refresh().refresh_modes)
        with self.assertRaises(HotAccountUnavailable):
            cache.lease(SYMBOLS)

    def test_defensive_copies_allow_local_revaluation_and_cannot_extend_authority(self):
        cache, wall, ticks = ready_cache()
        original = snapshot(fees={"XAUUSD1": Decimal("0.0004")})
        self.assertTrue(cache.publish(cache.begin_refresh(), original, 100))
        original.fees["XAUUSD1"] = Decimal("100")
        first, second = cache.lease(SYMBOLS), cache.lease(SYMBOLS)
        first.snapshot = replace(first.snapshot, equity=Decimal("50"))
        first.snapshot.fees.clear()
        self.assertEqual(second.snapshot.equity, Decimal("100"))
        self.assertEqual(second.snapshot.fees, {"XAUUSD1": Decimal("0.0004")})
        first.require_fresh()
        with self.assertRaises(AttributeError):
            first.started_monotonic = 9999
        ticks.return_value = 108.001
        first.snapshot.timestamp = NOW + 8
        with self.assertRaises(HotAccountUnavailable):
            first.require_fresh()
        with self.assertRaises(HotAccountUnavailable):
            second.require_fresh()

    def test_every_account_event_revokes_held_leases_and_inflight_refresh(self):
        cache, _, _ = ready_cache()
        lease = cache.lease(SYMBOLS)
        ticket = cache.begin_refresh()
        cache.invalidate()
        self.assertFalse(cache.publish(ticket, snapshot(), 100))
        with self.assertRaises(HotAccountUnavailable):
            lease.require_fresh()
        self.assertTrue(cache.publish(cache.begin_refresh(), snapshot(), 100))
        cache.lease(SYMBOLS).require_fresh()

    def test_event_during_publication_copy_is_not_blocked_and_rejects_old_generation(self):
        cache, _, _ = ready_cache()
        entered, release = threading.Event(), threading.Event()
        ticket = cache.begin_refresh()
        result = []

        def slow_copy(value):
            entered.set()
            if not release.wait(2):
                raise TimeoutError("Test publication was not released")
            return deepcopy(value)

        with patch("trading.account_cache.deepcopy", side_effect=slow_copy):
            publisher = threading.Thread(target=lambda: result.append(cache.publish(ticket, snapshot(), 100)))
            publisher.start()
            self.assertTrue(entered.wait(1))
            invalidator = threading.Thread(target=cache.invalidate)
            invalidator.start()
            invalidator.join(1)
            try:
                self.assertFalse(invalidator.is_alive())
            finally:
                release.set()
                publisher.join(1)
        self.assertFalse(publisher.is_alive())
        self.assertEqual(result, [False])
        with self.assertRaises(HotAccountUnavailable):
            cache.lease(SYMBOLS)

    def test_disconnect_and_reconnect_require_new_modes_and_new_snapshot(self):
        cache, _, _ = ready_cache()
        lease = cache.lease(SYMBOLS)
        ticket = cache.begin_refresh()
        cache.set_connected(False)
        cache.set_connected(True)
        self.assertFalse(cache.publish(ticket, snapshot(), 100))
        with self.assertRaises(HotAccountUnavailable):
            lease.require_fresh()
        fresh = cache.begin_refresh()
        self.assertTrue(fresh.refresh_modes)
        self.assertTrue(cache.publish(fresh, snapshot(), 100))
        self.assertFalse(cache.begin_refresh().refresh_modes)

    def test_configuration_change_rejects_old_symbols_and_inflight_result(self):
        cache, _, _ = ready_cache()
        lease = cache.lease(SYMBOLS)
        ticket = cache.begin_refresh()
        self.assertFalse(cache.configure(SYMBOLS))
        lease.require_fresh()
        self.assertTrue(cache.configure(("SPCXUSD1",)))
        self.assertFalse(cache.publish(ticket, snapshot(), 100))
        with self.assertRaises(HotAccountUnavailable):
            lease.require_fresh()
        with self.assertRaises(HotAccountUnavailable):
            cache.lease(SYMBOLS)
        fresh = cache.begin_refresh()
        self.assertEqual(fresh.symbols, ("SPCXUSD1",))
        self.assertTrue(fresh.refresh_modes)

    def test_periodic_read_keeps_existing_lease_until_publication_then_revokes(self):
        cache, _, _ = ready_cache()
        lease = cache.lease(SYMBOLS)
        ticket = cache.begin_refresh()
        lease.require_fresh()
        self.assertTrue(cache.publish(ticket, snapshot(equity=Decimal("90")), 100))
        with self.assertRaises(HotAccountUnavailable):
            lease.require_fresh()
        self.assertEqual(cache.lease(SYMBOLS).snapshot.equity, Decimal("90"))

    def test_out_of_order_refresh_and_foreign_tickets_cannot_replace_or_clear(self):
        cache, _, _ = ready_cache()
        other, _, _ = ready_cache()
        old, newer = cache.begin_refresh(), cache.begin_refresh()
        self.assertFalse(cache.publish(old, snapshot(), 100))
        self.assertFalse(cache.publish(other.begin_refresh(), snapshot(), 100))
        self.assertTrue(cache.publish(newer, snapshot(equity=Decimal("80")), 100))
        cache.fail(old, RuntimeError("obsolete failure"))
        self.assertEqual(cache.lease(SYMBOLS).snapshot.equity, Decimal("80"))
        self.assertFalse(cache.publish(None, snapshot(), 100))

    def test_latest_failure_revokes_lease_without_disclosing_raw_error(self):
        cache, _, _ = ready_cache()
        lease = cache.lease(SYMBOLS)
        cache.fail(cache.begin_refresh(), RuntimeError("SECRET_SIGNED_QUERY"))
        with self.assertRaises(HotAccountUnavailable) as result:
            cache.lease(SYMBOLS)
        self.assertNotIn("SECRET", str(result.exception))
        self.assertEqual(result.exception.retry_after, 1)
        with self.assertRaises(HotAccountUnavailable):
            lease.require_fresh()

    def test_rest_time_counts_toward_age_and_expiry_cannot_be_reversed(self):
        cache, wall, ticks = ready_cache()
        ticket = cache.begin_refresh()
        ticks.return_value = 107
        wall.return_value = NOW + 7
        self.assertTrue(cache.publish(ticket, snapshot(), 100))
        lease = cache.lease(SYMBOLS)
        ticks.return_value = 108
        wall.return_value = NOW + 8
        lease.require_fresh()
        ticks.return_value = 108.001
        with self.assertRaises(HotAccountUnavailable):
            lease.require_fresh()
        ticks.return_value = 100
        wall.return_value = NOW
        with self.assertRaises(HotAccountUnavailable):
            cache.lease(SYMBOLS)

    def test_mode_deadline_cannot_be_renewed_by_new_account_snapshot(self):
        cache, wall, ticks = ready_cache()
        self.assertTrue(cache.publish(cache.begin_refresh(), snapshot(), 100, valid_until_monotonic=103))
        ticks.return_value = 102
        wall.return_value = NOW + 2
        self.assertTrue(cache.publish(cache.begin_refresh(), snapshot(timestamp=NOW + 2), 102,
                                      valid_until_monotonic=103))
        lease = cache.lease(SYMBOLS)
        ticks.return_value = 103
        with self.assertRaises(HotAccountUnavailable):
            lease.require_fresh()

    def test_original_snapshot_and_tier_timestamps_survive_copy_mutations(self):
        cache, wall, ticks = ready_cache()
        with patch("trading.models.time.monotonic", side_effect=lambda: ticks.return_value):
            self.assertTrue(cache.publish(cache.begin_refresh(), snapshot(cycle_cap_cached_at={"XAUUSD1": 98}), 100))
            lease = cache.lease(SYMBOLS)
            lease.snapshot.cycle_cap_cached_at.clear()
            lease.require_fresh()
            ticks.return_value = 103
            with self.assertRaises(HotAccountUnavailable):
                lease.require_fresh()

    def test_slow_future_nonfinite_and_stale_snapshots_are_never_published(self):
        for start, stamp, deadline in ((91, NOW, None), (101, NOW, None),
                (float("nan"), NOW, None), (float("inf"), NOW, None),
                (True, NOW, None), (100, NOW - 9, None), (100, NOW + 2, None),
                (100, NOW, 100), (100, NOW, float("nan"))):
            with self.subTest(start=start, stamp=stamp, deadline=deadline):
                cache, _, _ = ready_cache()
                self.assertFalse(cache.publish(cache.begin_refresh(), snapshot(timestamp=stamp), start,
                    valid_until_monotonic=deadline))
                with self.assertRaises(HotAccountUnavailable):
                    cache.lease(SYMBOLS)

    def test_notifications_coalesce_but_event_during_read_schedules_another_read(self):
        listener = Mock()
        cache, _, _ = ready_cache(on_invalidate=listener)
        listener.reset_mock()
        cache.invalidate()
        cache.invalidate(refresh_modes=True)
        cache.invalidate()
        listener.assert_called_once_with()
        ticket = cache.begin_refresh()
        self.assertTrue(ticket.refresh_modes)
        cache.invalidate()
        self.assertEqual(listener.call_count, 2)
        self.assertFalse(cache.publish(ticket, snapshot(), 100))
        fresh = cache.begin_refresh()
        self.assertTrue(fresh.refresh_modes)
        self.assertTrue(cache.publish(fresh, snapshot(), 100))
        self.assertFalse(cache.begin_refresh().refresh_modes)

    def test_listener_can_reenter_cache_and_be_replaced_without_deadlock(self):
        cache, _, _ = ready_cache()
        observed = []

        def listener():
            observed.append(cache.configure(SYMBOLS))

        cache.set_listener(listener)
        thread = threading.Thread(target=cache.invalidate)
        thread.start()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(observed, [False])
        replacement = Mock()
        cache.set_listener(replacement)
        replacement.assert_called_once_with()

    def test_expired_read_does_not_discard_an_inflight_replacement(self):
        cache, wall, ticks = ready_cache()
        ticket = cache.begin_refresh()
        ticks.return_value = 109
        wall.return_value = NOW + 9
        with self.assertRaises(HotAccountUnavailable):
            cache.lease(SYMBOLS)
        self.assertTrue(cache.publish(ticket, snapshot(timestamp=NOW + 9), 109))
        cache.lease(SYMBOLS).require_fresh()

    def test_configuration_empty_disables_new_refresh_and_repeated_connect_revokes(self):
        cache, _, _ = ready_cache()
        lease = cache.lease(SYMBOLS)
        cache.set_connected(True)
        with self.assertRaises(HotAccountUnavailable):
            lease.require_fresh()
        self.assertTrue(cache.configure(()))
        with self.assertRaises(HotAccountUnavailable):
            cache.begin_refresh()


if __name__ == "__main__":
    unittest.main()
