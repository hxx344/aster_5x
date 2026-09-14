"""Hot broker reads remain local while background refreshes and writes race."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from fractions import Fraction
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from trading.account_cache import HotAccountUnavailable
from trading.depth import DepthSnapshot
from trading.exchange import ExchangeError, LiveBroker, MarketData, RequestNotSent
from trading.models import AccountModeError, TradingError, dec
from .test_cycle_account_snapshot import ACCOUNT, BRACKET, MULTI, RISK, SYMBOL, LocalQuoteMarket, account_row, cycle_account_responses, risk_row
from .test_exchange_hardening import FixtureAPI
from trading.paper import PAPER_BRACKETS


class CycleLocalMarketTests(unittest.TestCase):
    def make_market(self):
        quotes = LocalQuoteMarket()
        stream = SimpleNamespace(book=quotes._stream_book)
        depth = DepthSnapshot(((Fraction(99), Fraction(100)),), ((Fraction(101), Fraction(100)),), time.time())
        depth_stream = SimpleNamespace(snapshot=Mock(return_value=depth), seed_token=Mock())
        api = SimpleNamespace(call=Mock(side_effect=AssertionError("hot path performed HTTP")))
        return MarketData(api, stream=stream, depth_stream=depth_stream), quotes, depth

    def test_complete_local_book_and_depth_do_not_make_http_requests(self):
        market, _, depth = self.make_market()
        self.assertEqual(market.cycle_book(SYMBOL).mark, dec(100))
        self.assertIs(market.cycle_depth(SYMBOL), depth)
        market.api.call.assert_not_called()
        market.depth_stream.seed_token.assert_not_called()

    def test_missing_or_stale_local_data_never_falls_back_to_rest(self):
        market, quotes, depth = self.make_market()
        quotes.connected.clear()
        market.depth_stream.snapshot.return_value = None
        with self.assertRaises(HotAccountUnavailable):
            market.cycle_book(SYMBOL)
        with self.assertRaises(HotAccountUnavailable):
            market.cycle_depth(SYMBOL)
        market.stream.book = lambda symbol: replace(quotes._quote(symbol), timestamp=time.time() - 4)
        market.depth_stream.snapshot.return_value = replace(depth, timestamp=time.time() - 16)
        with self.assertRaises(TradingError):
            market.cycle_book(SYMBOL)
        with self.assertRaises(TradingError):
            market.cycle_depth(SYMBOL)
        market.api.call.assert_not_called()
        market.depth_stream.seed_token.assert_not_called()

    def test_revoked_depth_generation_is_not_reseeded_by_trading_read(self):
        market, _, depth = self.make_market()
        market.depth_stream.snapshot.return_value = replace(depth, validity=lambda: False)
        with self.assertRaises(TradingError):
            market.cycle_depth(SYMBOL)
        market.api.call.assert_not_called()
        market.depth_stream.seed_token.assert_not_called()


class CycleHotBrokerTests(unittest.TestCase):
    def make_broker(self, *, holding=False):
        responses = cycle_account_responses()
        if holding:
            for row, amount in zip(responses[ACCOUNT]["positions"], ("2", "-1")):
                row.update(positionAmt=amount, entryPrice="100")
        api = FixtureAPI(responses)
        api.close = Mock()
        market = LocalQuoteMarket()
        return LiveBroker({}, market, api=api), api, market

    def warm(self, broker):
        broker.cycle_cache.configure([SYMBOL])
        broker.cycle_cache.set_connected(True)
        self.assertTrue(broker.refresh_cycle_hot_snapshot())

    def test_unprepared_hot_cache_returns_unavailable_without_http(self):
        broker, api, _ = self.make_broker()
        self.assertIsNone(broker.cycle_stream)
        with self.assertRaises(HotAccountUnavailable):
            broker.cycle_hot_snapshot([SYMBOL])
        self.assertEqual(api.calls, [])

    def test_background_refresh_is_only_network_reader_and_never_queries_orders(self):
        broker, api, _ = self.make_broker()
        self.warm(broker)
        self.assertCountEqual([call[1] for call in api.calls], [ACCOUNT, MULTI])
        self.assertIsNone(broker.leverage_snapshot)
        api.calls.clear()
        lease = broker.cycle_hot_snapshot([SYMBOL])
        self.assertIsNone(lease.snapshot.open_orders)
        self.assertEqual(api.calls, [])
        self.assertTrue(broker.refresh_cycle_hot_snapshot())
        self.assertEqual([call[1] for call in api.calls], [ACCOUNT])

    def test_latest_marks_revalue_all_margin_and_charge_losses_without_renewing_source(self):
        broker, api, market = self.make_broker(holding=True)
        self.warm(broker)
        original = broker.cycle_hot_snapshot([SYMBOL])
        api.calls.clear()
        market.marks[SYMBOL] = dec(90)
        loss = broker.cycle_hot_snapshot([SYMBOL])
        self.assertEqual(loss.snapshot.unrealized, dec(-10))
        self.assertEqual(loss.snapshot.equity, dec(190))
        self.assertEqual(loss.snapshot.available, dec(140))
        self.assertEqual(loss.snapshot.occupied_margin_exact, Fraction(54))
        self.assertEqual(loss.snapshot.timestamp, original.snapshot.timestamp)
        self.assertEqual(loss.started_monotonic, original.started_monotonic)
        market.marks[SYMBOL] = dec(120)
        gain = broker.cycle_hot_snapshot([SYMBOL])
        self.assertEqual(gain.snapshot.unrealized, dec(0))
        self.assertEqual(gain.snapshot.equity, dec(200))
        self.assertEqual(gain.snapshot.available, dec(150))
        self.assertEqual(gain.snapshot.occupied_margin_exact, Fraction(72))
        self.assertEqual(gain.snapshot.timestamp, original.snapshot.timestamp)
        self.assertEqual(api.calls, [])
        self.assertEqual(original.snapshot.positions[0].mark, dec(100))
        gain.snapshot.positions[0].qty = dec(999)
        self.assertEqual(broker.cycle_hot_snapshot([SYMBOL]).snapshot.positions[0].qty, dec(2))

    def test_missing_local_outside_mark_keeps_background_mark_without_any_query(self):
        broker, api, market = self.make_broker()
        outside = account_row("OTHERUSD1", amount="2", entry="100")
        api.responses[ACCOUNT]["positions"].append(outside)
        api.responses[RISK] = [risk_row(outside, mark="80")]
        market.assets["OTHERUSD1"] = "USD1"
        self.warm(broker)
        before = broker.cycle_hot_snapshot([SYMBOL])
        api.calls.clear()
        after = broker.cycle_hot_snapshot([SYMBOL])
        self.assertEqual(after.snapshot.positions, before.snapshot.positions)
        self.assertEqual(next(p.mark for p in after.snapshot.positions if p.symbol == "OTHERUSD1"), dec(80))
        self.assertEqual(after.snapshot.timestamp, before.snapshot.timestamp)
        self.assertEqual(api.calls, [])
        self.assertEqual(market.public_reads, [])

    def test_hot_repricing_keeps_account_tier_and_mode_expiries(self):
        for source, published_at, expired_at in (("account", 100, 108.001), ("tier", 104.9, 105.1), ("mode", 114.9, 115.1)):
            with self.subTest(source=source):
                clock = SimpleNamespace(mono=100.0, wall=1000.0)
                broker, api, _ = self.make_broker()
                if source == "tier":
                    for row in api.responses[ACCOUNT]["positions"]:
                        del row["maxNotional"]
                    api.responses[BRACKET] = {"symbol": SYMBOL, "brackets": PAPER_BRACKETS}
                with patch("trading.exchange.time.monotonic", side_effect=lambda: clock.mono), \
                     patch("trading.exchange.time.time", side_effect=lambda: clock.wall):
                    self.warm(broker)
                    if published_at != 100:
                        clock.mono, clock.wall = published_at, 900 + published_at
                        self.assertTrue(broker.refresh_cycle_hot_snapshot())
                    lease = broker.cycle_hot_snapshot([SYMBOL])
                    if source == "tier":
                        self.assertEqual(lease.snapshot.cycle_cap_cached_at, {SYMBOL: 100})
                    api.calls.clear()
                    clock.mono, clock.wall = expired_at, 900 + expired_at
                    with self.assertRaises(HotAccountUnavailable):
                        broker.cycle_hot_snapshot([SYMBOL])
                    with self.assertRaises(HotAccountUnavailable):
                        lease.require_fresh()
                    self.assertEqual(api.calls, [])

    def test_private_event_revokes_lease_and_config_event_forces_background_mode_refresh(self):
        broker, api, _ = self.make_broker()
        self.warm(broker)
        for event, expected in (("ACCOUNT_UPDATE", [ACCOUNT]), ("ACCOUNT_CONFIG_UPDATE", [ACCOUNT, MULTI])):
            with self.subTest(event=event):
                lease = broker.cycle_hot_snapshot([SYMBOL])
                api.calls.clear()
                broker._cycle_account_event(event)
                with self.assertRaises(HotAccountUnavailable):
                    lease.require_fresh()
                with self.assertRaises(HotAccountUnavailable):
                    broker.cycle_hot_snapshot([SYMBOL])
                self.assertEqual(api.calls, [])
                self.assertTrue(broker.refresh_cycle_hot_snapshot())
                self.assertCountEqual([call[1] for call in api.calls], expected)

    def test_failed_refresh_is_propagated_and_cannot_keep_a_usable_lease(self):
        broker, api, _ = self.make_broker()
        self.warm(broker)
        lease = broker.cycle_hot_snapshot([SYMBOL])
        error = RequestNotSent("background request failed")
        api.responses[ACCOUNT] = error
        with self.assertRaises(RequestNotSent) as caught:
            broker.refresh_cycle_hot_snapshot()
        self.assertIs(caught.exception, error)
        with self.assertRaises(HotAccountUnavailable):
            lease.require_fresh()
        self.assertIsNone(broker.leverage_snapshot)

    def test_background_multi_asset_mode_error_is_raised_before_publish(self):
        broker, api, _ = self.make_broker()
        self.warm(broker)
        api.responses[MULTI]["multiAssetsMargin"] = True
        broker.invalidate_cycle_hot_data("mode changed", refresh_modes=True)
        with patch.object(broker.cycle_cache, "publish", wraps=broker.cycle_cache.publish) as publish:
            with self.assertRaises(AccountModeError):
                broker.refresh_cycle_hot_snapshot()
        publish.assert_not_called()
        with self.assertRaises(HotAccountUnavailable):
            broker.cycle_hot_snapshot([SYMBOL])
        self.assertIsNone(broker.leverage_snapshot)

    def test_hot_reads_and_submit_do_not_wait_for_background_http_lock(self):
        broker, api, _ = self.make_broker()
        self.warm(broker)
        entered, release, posted = threading.Event(), threading.Event(), threading.Event()
        original = api.call
        def call(method, path, *args, **kwargs):
            if method == "POST":
                posted.set()
                return {"orderId": 1}
            if path == ACCOUNT:
                entered.set()
                self.assertTrue(release.wait(3))
            return original(method, path, *args, **kwargs)
        api.call = call
        with ThreadPoolExecutor(max_workers=3) as pool:
            refresh = pool.submit(broker.refresh_cycle_hot_snapshot)
            self.assertTrue(entered.wait(3))
            read = pool.submit(broker.cycle_hot_snapshot, [SYMBOL])
            try:
                lease = read.result(timeout=1)
                write = pool.submit(broker.submit, [{"symbol": SYMBOL}])
                self.assertTrue(posted.wait(1))
                self.assertEqual(write.result(timeout=1), [{"orderId": 1}])
                with self.assertRaises(HotAccountUnavailable):
                    lease.require_fresh()
            finally:
                release.set()
            self.assertFalse(refresh.result(timeout=3))
        with self.assertRaises(HotAccountUnavailable):
            broker.cycle_hot_snapshot([SYMBOL])

    def test_refresh_started_during_submit_is_revoked_when_write_finishes(self):
        broker, api, _ = self.make_broker()
        self.warm(broker)
        entered, release = threading.Event(), threading.Event()
        original = api.call
        def call(method, path, *args, **kwargs):
            if method == "POST":
                entered.set()
                self.assertTrue(release.wait(3))
                return {"orderId": 1}
            return original(method, path, *args, **kwargs)
        api.call = call
        with ThreadPoolExecutor(max_workers=1) as pool:
            write = pool.submit(broker.submit, [{"symbol": SYMBOL}])
            self.assertTrue(entered.wait(3))
            try:
                self.assertTrue(broker.refresh_cycle_hot_snapshot())
                during = broker.cycle_hot_snapshot([SYMBOL])
            finally:
                release.set()
            write.result(timeout=3)
        with self.assertRaises(HotAccountUnavailable):
            during.require_fresh()

    def test_cancel_revokes_hot_lease_before_sending_delete(self):
        broker, api, _ = self.make_broker()
        self.warm(broker)
        lease = broker.cycle_hot_snapshot([SYMBOL])
        def call(method, path, *args, **kwargs):
            self.assertEqual(method, "DELETE")
            with self.assertRaises(HotAccountUnavailable):
                lease.require_fresh()
            return {"status": "CANCELED"}
        api.call = call
        self.assertEqual(broker.cancel(SYMBOL, "test-order"), {"status": "CANCELED"})

    def test_both_leverage_writes_revoke_leases_and_request_fresh_modes(self):
        for setter, leverage in (("set_leverage", 10), ("set_cycle_leverage", 6)):
            with self.subTest(setter=setter):
                broker, api, _ = self.make_broker()
                self.warm(broker)
                lease = broker.cycle_hot_snapshot([SYMBOL])
                original = api.call
                def call(method, path, params=None, **kwargs):
                    if method == "POST":
                        with self.assertRaises(HotAccountUnavailable):
                            lease.require_fresh()
                        return {"symbol": SYMBOL, "leverage": leverage}
                    return original(method, path, params, **kwargs)
                api.call = call
                # Ordinary qualification retains its own synchronous contract.
                with patch.object(broker, "snapshot", return_value=lease.snapshot):
                    result = getattr(broker, setter)(SYMBOL, leverage)
                self.assertEqual(result["leverage"], leverage)
                api.calls.clear()
                self.assertTrue(broker.refresh_cycle_hot_snapshot())
                self.assertCountEqual([call[1] for call in api.calls], [ACCOUNT, MULTI])

    def test_failed_write_does_not_leave_old_hot_data_usable_or_retry_post(self):
        broker, api, _ = self.make_broker()
        self.warm(broker)
        lease = broker.cycle_hot_snapshot([SYMBOL])
        error = ExchangeError("mock rejection")
        api.call = Mock(side_effect=error)
        with self.assertRaises(ExchangeError) as caught:
            broker.submit([{"symbol": SYMBOL}])
        self.assertIs(caught.exception, error)
        api.call.assert_called_once()
        with self.assertRaises(HotAccountUnavailable):
            lease.require_fresh()

    def test_stop_revokes_state_but_allows_a_new_stream_without_closing_api(self):
        broker, api, _ = self.make_broker()
        streams = [Mock(), Mock()]
        with patch("trading.exchange.PrivateAccountStream", side_effect=streams) as factory:
            broker.start_cycle_hot_data([SYMBOL])
            self.warm(broker)
            lease = broker.cycle_hot_snapshot([SYMBOL])
            broker.stop_cycle_hot_data()
            with self.assertRaises(HotAccountUnavailable):
                lease.require_fresh()
            self.assertIsNone(broker.cycle_stream)
            api.close.assert_not_called()
            broker.start_cycle_hot_data([SYMBOL])
        self.assertEqual(factory.call_count, 2)
        streams[0].close.assert_called_once()
        self.assertIs(broker.cycle_stream, streams[1])

    def test_start_uses_one_stream_and_close_stops_it_before_api(self):
        broker, api, _ = self.make_broker()
        order = []
        api.close.side_effect = lambda: order.append("api")
        stream = Mock()
        stream.close.side_effect = lambda: order.append("stream")
        with patch("trading.exchange.PrivateAccountStream", return_value=stream) as factory:
            broker.start_cycle_hot_data([SYMBOL])
            broker.start_cycle_hot_data([SYMBOL])
        factory.assert_called_once()
        self.assertEqual(api.calls, [])
        broker.close()
        self.assertEqual(order, ["stream", "api"])
        with self.assertRaises(HotAccountUnavailable):
            broker.start_cycle_hot_data([SYMBOL])

    def test_restarted_private_stream_ignores_previous_stream_callbacks(self):
        broker, api, _ = self.make_broker()
        streams = []
        def make_stream(api, *, on_state, on_event):
            stream = SimpleNamespace(on_state=on_state, on_event=on_event,
                start=Mock(side_effect=lambda: on_state(True)),
                close=Mock(side_effect=lambda: on_state(False)))
            streams.append(stream)
            return stream
        with patch("trading.exchange.PrivateAccountStream", side_effect=make_stream):
            broker.start_cycle_hot_data([SYMBOL])
            broker.stop_cycle_hot_data()
            broker.start_cycle_hot_data([SYMBOL])
        self.assertTrue(broker.refresh_cycle_hot_snapshot())
        lease = broker.cycle_hot_snapshot([SYMBOL])
        api.calls.clear()
        streams[0].on_state(False)
        streams[0].on_state(True)
        streams[0].on_event("ACCOUNT_UPDATE")
        streams[0].on_event("ACCOUNT_CONFIG_UPDATE")
        lease.require_fresh()
        broker.cycle_hot_snapshot([SYMBOL]).require_fresh()
        self.assertEqual(api.calls, [])
        streams[1].on_event("ACCOUNT_UPDATE")
        with self.assertRaises(HotAccountUnavailable):
            lease.require_fresh()
        self.assertTrue(broker.refresh_cycle_hot_snapshot())
        self.assertEqual([call[1] for call in api.calls], [ACCOUNT])

    def test_stop_silences_disconnect_notifications_and_restart_restores_listener(self):
        broker, _, _ = self.make_broker()
        listener = Mock()
        streams = [Mock(), Mock()]
        streams[0].close.side_effect = lambda: broker.cycle_cache.set_connected(False)
        with patch("trading.exchange.PrivateAccountStream", side_effect=streams):
            broker.start_cycle_hot_data([SYMBOL], on_invalidate=listener)
            self.warm(broker)
            listener.reset_mock()
            broker.stop_cycle_hot_data()
            broker.stop_cycle_hot_data()
            broker.cycle_cache.set_connected(False)
            listener.assert_not_called()
            broker.start_cycle_hot_data([SYMBOL], on_invalidate=listener)
            listener.assert_called_once()


if __name__ == "__main__":
    unittest.main()
