from contextlib import ExitStack
from copy import deepcopy
from fractions import Fraction
import unittest
from unittest.mock import Mock, patch

from tests.helpers import Fixture
from trading.cycle import DEFAULT_CYCLE
from trading.depth import DepthSnapshot
from trading.engine import Engine
from trading.exchange import MarketData
from trading.models import Book, dec


SYMBOL = "XAUUSD1"
OTHER = "SPCXUSD1"
WALL = 1_800_000_000


class _Future:
    def __init__(self, value=5, *, done=True, error=None):
        self.value, self.complete, self.error = value, done, error

    def done(self):
        return self.complete

    def result(self):
        if self.error is not None:
            raise self.error
        return self.value


class _Pool:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def submit(self, function, *args, **kwargs):
        result = function(*args, **kwargs)
        return result if isinstance(result, _Future) else _Future(result)


class _SchedulerEvent:
    def __init__(self, harness):
        self.harness = harness
        self.flag = False
        self.set_count = 0

    def clear(self):
        self.flag = False

    def set(self):
        self.flag = True
        self.set_count += 1

    def wait(self, timeout):
        run = self.harness
        run.iteration += 1
        try:
            if run.iteration > 16:
                raise AssertionError("deterministic scheduler did not stop")
            run.control(run.iteration)
        except BaseException as exc:
            run.failure = exc
            run.engine.shutdown.set()
        return self.flag


class _Harness:
    """Run the real scheduler with controlled futures and no thread or network."""

    def __init__(self, fixture):
        self.fixture = fixture
        self.ticks = 100
        self.iteration = 0
        self.failure = None
        self.calls = []
        self.timing = {"test": {"interval": 30, "gap": 30}}
        self.on_submit = lambda aid, signal: _Future()
        self.control = lambda iteration: self.engine.shutdown.set()
        self.quote = Mock(spec=["book", "start", "close", "set_update_listener"])
        self.depth = Mock(spec=["snapshot", "start", "close", "set_update_listener"])
        self.listeners = {}
        self.quote.set_update_listener.side_effect = lambda value: self.listeners.update(bbo=value)
        self.depth.set_update_listener.side_effect = lambda value: self.listeners.update(depth=value)
        self.api = Mock()
        self.market = MarketData(api=self.api, stream=self.quote, depth_stream=self.depth)
        self.market.rules = fixture.market.rules
        row = deepcopy(fixture.account)
        row["policy"]["symbols"] = [OTHER]
        row["cycle"] = {**DEFAULT_CYCLE, "enabled": True, "spread_notional": "1000",
                        "max_notional": "1000"}
        fixture.store.save_account(row)
        self.engine = Engine(fixture.store, market=self.market)
        self.engine.ready = True
        self.event = _SchedulerEvent(self)
        self.engine.scheduler_event = self.event
        self.refresh()

    @property
    def wall(self):
        return WALL + self.ticks - 100

    def refresh(self, *, good=True):
        self.engine.markets[SYMBOL] = {"status": "ok", "checked_at": self.wall, "capacities": {"5": "1000000"}}
        self.engine.view("test", snapshot={"timestamp": self.wall, "positions": [{"symbol": SYMBOL, "leverage": 5}]})
        ask = "100" if good else "101"
        self.quote.book.return_value = Book(dec("100"), dec(ask), dec("1000"), dec("1000"), dec("100"), self.wall)
        self.depth.snapshot.return_value = DepthSnapshot(((Fraction(100), Fraction(1000)),),
            ((Fraction(ask), Fraction(1000)),), self.wall)

    def emit(self, at, *, source="bbo", symbol=SYMBOL, good=True):
        self.ticks = at
        self.refresh(good=good)
        self.listeners[source](symbol, source, self.wall, self.ticks)

    def submit_account(self, aid, *, cycle_signal=None):
        if any(call["aid"] == aid and not call["future"].done() for call in self.calls):
            raise AssertionError("overlapping workers for one account")
        future = self.on_submit(aid, cycle_signal)
        self.calls.append({"aid": aid, "at": self.ticks, "signal": cycle_signal,
                           "priority": deepcopy(self.engine.active_priority_signals.get(aid)), "future": future})
        return future

    def run(self, control):
        self.control = control
        with ExitStack() as stack:
            stack.enter_context(patch("trading.engine.ThreadPoolExecutor", return_value=_Pool()))
            stack.enter_context(patch("trading.engine.time.monotonic", side_effect=lambda: self.ticks))
            stack.enter_context(patch("trading.engine.time.time", side_effect=lambda: self.wall))
            stack.enter_context(patch.object(self.engine, "scheduling", return_value=self.timing))
            stack.enter_context(patch.object(self.engine, "tick_account", side_effect=self.submit_account))
            for name in ("poll_market", "poll_book", "poll_depth", "notify"):
                stack.enter_context(patch.object(self.engine, name, return_value=60))
            self.engine.run()
        if self.failure is not None:
            raise self.failure

    def fast_calls(self):
        return [call for call in self.calls if call["signal"] is not None]


class CycleWSSchedulerTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.h = _Harness(self.f)

    def test_callback_wakes_real_run_before_ordinary_due_and_shared_gap(self):
        h = self.h

        def control(step):
            if step == 2:
                h.emit(101)
            elif step == 3:
                h.engine.shutdown.set()

        h.run(control)
        self.assertEqual([(call["at"], call["signal"] is not None) for call in h.calls], [(100, False), (101, True)])
        self.assertEqual(h.fast_calls()[0]["signal"]["source"], "bbo")
        self.assertGreaterEqual(h.event.set_count, 2)
        self.assertEqual(h.quote.set_update_listener.call_args_list[-1].args, (None,))
        self.assertEqual(h.depth.set_update_listener.call_args_list[-1].args, (None,))
        h.api.call.assert_not_called()

    def test_fast_completion_preserves_original_due_and_ordinary_turn(self):
        h = self.h

        def control(step):
            if step == 2:
                h.emit(101)
            elif step == 3:
                h.ticks = 129
            elif step == 4:
                h.emit(130)
            elif step == 5:
                h.engine.shutdown.set()

        h.run(control)
        self.assertEqual([(call["at"], call["signal"] is not None) for call in h.calls],
                         [(100, False), (101, True), (130, False)])

    def test_busy_account_keeps_only_latest_signal_then_handles_it_once(self):
        h = self.h
        held = _Future(done=False)
        fast_count = []

        def submit(aid, signal):
            if signal is not None:
                fast_count.append(signal)
                if len(fast_count) == 1:
                    return held
            return _Future()

        h.on_submit = submit

        def control(step):
            if step == 2:
                h.emit(101)
            elif step == 3:
                h.emit(101.1, source="depth")
            elif step == 4:
                h.emit(101.2)
            elif step == 5:
                h.ticks = 102
                held.complete = True
            elif step == 6:
                h.engine.shutdown.set()

        h.run(control)
        self.assertEqual(len(h.calls), 3)
        self.assertEqual([call["at"] for call in h.fast_calls()], [101, 102])
        self.assertEqual([call["signal"]["received_monotonic"] for call in h.fast_calls()], [101, 101.2])
        self.assertEqual(h.fast_calls()[1]["signal"]["source"], "bbo")

    def test_minimum_fast_interval_keeps_latest_deferred_update(self):
        h = self.h
        held = _Future(done=False)
        h.on_submit = lambda aid, signal: held if signal is not None and not h.fast_calls() else _Future()

        def control(step):
            if step == 2:
                h.emit(101)
            elif step == 3:
                h.emit(101.1)
            elif step == 4:
                held.complete = True
                h.ticks = 101.2
            elif step == 5:
                h.emit(101.5, source="depth")
            elif step == 6:
                h.ticks = 102
            elif step == 7:
                h.engine.shutdown.set()

        h.run(control)
        self.assertEqual([call["at"] for call in h.fast_calls()], [101, 102])
        self.assertEqual(h.fast_calls()[1]["signal"]["received_monotonic"], 101.5)

    def test_update_followed_by_immediate_completion_is_not_lost_between_loops(self):
        h = self.h
        held = _Future(done=False)
        h.on_submit = lambda aid, signal: held if signal is not None and not h.fast_calls() else _Future()

        def control(step):
            if step == 2:
                h.emit(101)
            elif step == 3:
                h.emit(101.5, source="depth")
                held.complete = True
                self.assertNotIn("test", h.engine.cycle_signal_deferred)
            elif step == 4:
                h.ticks = 102
            elif step == 5:
                h.engine.shutdown.set()

        h.run(control)
        self.assertEqual([call["at"] for call in h.fast_calls()], [101, 102])
        self.assertEqual(h.fast_calls()[1]["signal"]["received_monotonic"], 101.5)

    def test_continuous_fast_updates_cannot_postpone_the_ordinary_due_time(self):
        h = self.h
        h.timing["test"] = {"interval": 6, "gap": 6}

        def control(step):
            if 2 <= step <= 7:
                h.emit(99 + step)
            elif step == 8:
                h.engine.shutdown.set()

        h.run(control)
        self.assertEqual([(call["at"], call["signal"] is not None) for call in h.calls],
            [(100, False), (101, True), (102, True), (103, True), (104, True), (105, True), (106, False)])

    def test_capacity_update_at_worker_completion_rechecks_the_same_hot_quote(self):
        h, held = self.h, _Future(done=False)
        h.on_submit = lambda aid, signal: held if signal is not None and not h.fast_calls() else _Future()
        def control(step):
            if step == 2:
                h.emit(101)
            elif step == 3:
                h.ticks = 101.5
                h.engine.markets[SYMBOL]["checked_at"] = h.wall
                held.complete = True
                self.assertNotIn("test", h.engine.cycle_signal_deferred)
            elif step == 4:
                h.ticks = 102
            elif step == 5:
                h.engine.shutdown.set()
        h.run(control)
        self.assertEqual([call["at"] for call in h.fast_calls()], [101, 102])

    def test_expired_update_received_while_busy_is_not_executed_after_completion(self):
        h = self.h
        held = _Future(done=False)
        h.on_submit = lambda aid, signal: held if signal is not None else _Future()

        def control(step):
            if step == 2:
                h.emit(101)
            elif step == 3:
                h.emit(101.2)
            elif step == 4:
                held.complete = True
                h.ticks = 105
            elif step == 5:
                h.engine.shutdown.set()

        h.run(control)
        self.assertEqual(len(h.fast_calls()), 1)

    def test_hard_and_stale_tag_backoff_cannot_be_bypassed_but_quote_wait_can(self):
        for tag in (None, 109, 110):
            with self.subTest(tag=tag):
                if tag is not None:
                    self.h = _Harness(self.f)
                h = self.h

                def control(step):
                    if step == 2:
                        h.engine.account_backoff["test"] = 110
                        if tag is not None:
                            h.engine.cycle_quote_backoff["test"] = tag
                        h.emit(101)
                    elif step == 3:
                        h.engine.shutdown.set()

                h.run(control)
                self.assertEqual(len(h.fast_calls()), 1 if tag == 110 else 0)

    def test_unexpected_ordinary_future_failure_blocks_new_ws_work(self):
        h = self.h
        h.on_submit = lambda aid, signal: _Future(error=RuntimeError("unexpected worker failure"))

        def control(step):
            if step == 2:
                h.emit(101)
            elif step == 3:
                h.engine.shutdown.set()

        with self.assertLogs("aster.trading", level="ERROR"):
            h.run(control)
        self.assertEqual(len(h.calls), 1)
        self.assertEqual(h.engine.account_backoff["test"], 130)
        self.assertNotIn("test", h.engine.cycle_quote_backoff)

    def test_unexpected_fast_future_failure_keeps_backoff_after_public_edge_changes(self):
        h = self.h
        h.on_submit = lambda aid, signal: _Future(error=RuntimeError("unexpected worker failure")) if signal else _Future()

        def control(step):
            if step == 2:
                h.emit(101)
            elif step == 3:
                h.emit(102, good=False)
            elif step == 4:
                h.emit(103)
            elif step == 5:
                h.engine.shutdown.set()

        with self.assertLogs("aster.trading", level="ERROR"):
            h.run(control)
        self.assertEqual(len(h.fast_calls()), 1)
        self.assertEqual(h.engine.account_backoff["test"], 132)
        self.assertNotIn("test", h.engine.cycle_quote_backoff)

    def test_fast_path_preserves_other_market_priority_signal(self):
        h = self.h

        def control(step):
            if step == 2:
                h.engine.priority_accounts["test"] = {OTHER: h.wall}
                h.engine.markets[OTHER] = {"status": "unknown"}
                h.emit(101)
            elif step == 3:
                h.engine.shutdown.set()

        h.run(control)
        self.assertEqual(len(h.fast_calls()), 1)
        self.assertEqual(h.engine.priority_accounts["test"], {OTHER: WALL})
        self.assertIsNone(h.fast_calls()[0]["priority"])

    def test_live_ordinary_priority_takes_precedence_over_cycle_fast_signal(self):
        h = self.h

        def control(step):
            if step == 2:
                h.emit(101)
                h.engine.priority_accounts["test"] = {OTHER: h.wall}
                h.engine.markets[OTHER] = {"status": "ok", "checked_at": h.wall}
            elif step == 3:
                h.engine.shutdown.set()

        h.run(control)
        self.assertEqual(len(h.calls), 2)
        self.assertEqual(len(h.fast_calls()), 0)
        self.assertEqual(h.calls[1]["priority"], {OTHER: WALL + 1})

    def test_pause_and_symbol_configuration_invalidate_old_cycle_signal(self):
        for change in ("pause", "cycle_off", "migration", "symbol"):
            with self.subTest(change=change):
                h = _Harness(self.f)

                def control(step):
                    if step == 2:
                        row = self.f.store.account("test")
                        if change == "pause":
                            row["enabled"] = False
                        elif change == "cycle_off":
                            row["cycle"]["enabled"] = False
                        elif change == "migration":
                            row["migration"] = {"enabled": True}
                        else:
                            row["cycle"]["symbol"] = "CLUSD1"
                        self.f.store.save_account(row)
                        h.engine.accounts_generation += 1
                        h.emit(101)
                    elif step == 3:
                        h.engine.shutdown.set()

                h.run(control)
                self.assertEqual(h.fast_calls(), [])

    def test_fast_reconfigure_and_resume_rearms_same_symbol_without_observing_pause(self):
        h = self.h

        def control(step):
            if step == 2:
                h.emit(101)
            elif step == 3:
                h.ticks = 101.5  # Complete the first attempt without another quote.
            elif step == 4:
                # Pause, configure and resume all finish between scheduler
                # iterations, so only the final enabled row is observed.
                row = self.f.store.account("test")
                row["cycle"]["min_notional"] = "200"
                self.f.store.save_account(row)
                h.engine.accounts_generation += 3
                h.engine.wake_accounts.add("test")
                h.emit(102)
            elif step == 5:
                h.engine.shutdown.set()

        h.run(control)
        self.assertEqual([call["at"] for call in h.fast_calls()], [101, 102])
        self.assertGreater(dec(h.fast_calls()[1]["signal"]["minimum_quantity"]),
                           dec(h.fast_calls()[0]["signal"]["minimum_quantity"]))

    def test_first_synchronized_depth_can_wake_after_a_newer_bbo_was_unusable(self):
        h = self.h

        def control(step):
            if step == 2:
                h.emit(101)
                h.depth.snapshot.return_value = None
            elif step == 3:
                h.ticks = 101.5
                h.refresh()
                # Seeding publishes the buffered event's original receive time,
                # which can predate the latest BBO but still be fully fresh.
                h.listeners["depth"](SYMBOL, "depth", WALL + 0.5, 100.5)
            elif step == 4:
                h.engine.shutdown.set()

        h.run(control)
        self.assertEqual([call["at"] for call in h.fast_calls()], [101.5])
        self.assertEqual(h.fast_calls()[0]["signal"]["source"], "depth")
        self.assertEqual(h.fast_calls()[0]["signal"]["received_monotonic"], 100.5)

    def test_source_watermarks_reject_old_events_after_other_source_updates(self):
        engine = self.h.engine
        with patch.object(self.f.store, "get", side_effect=AssertionError("receiver read database")), \
             patch.object(engine, "broker", side_effect=AssertionError("receiver reached broker")):
            engine.on_cycle_market_update(SYMBOL, "depth", WALL, 100)
            engine.on_cycle_market_update(SYMBOL, "bbo", WALL + 1, 101)
            engine.on_cycle_market_update(SYMBOL, "depth", WALL - 1, 99)
            self.assertEqual(engine.cycle_market_updates[SYMBOL]["source"], "bbo")
            engine.on_cycle_market_update(SYMBOL, "depth", WALL + 0.5, 100.5)
        self.assertEqual(engine.cycle_market_updates[SYMBOL]["source"], "depth")
        self.assertEqual(len(engine.cycle_market_updates), 1)

    def test_pending_batch_and_post_fill_marker_prevent_fast_submission(self):
        for marker in ("intent", "post_fill"):
            with self.subTest(marker=marker):
                fixture = Fixture()
                self.addCleanup(fixture.close)
                h = _Harness(fixture)

                def control(step):
                    if step == 2:
                        if marker == "intent":
                            fixture.store.save_intent({"id": "pending-test", "kind": "cycle", "account_id": "test",
                                "symbol": SYMBOL, "status": "pending"})
                        else:
                            fixture.store.put("post_fill_check:test", {"symbol": OTHER})
                        h.emit(101)
                    elif step == 3:
                        h.engine.shutdown.set()

                h.run(control)
                self.assertEqual(h.fast_calls(), [])

    def test_daily_rolling_and_attention_states_prevent_fast_submission(self):
        h = self.h

        def control(step):
            if step in (2, 3, 4):
                phase = {2: "daily_limit", 3: "rolling_limit", 4: "attention"}[step]
                h.engine.views["test"] = {"cycle_state": {"phase": phase}}
                h.emit(99 + step)
            elif step == 5:
                h.engine.shutdown.set()

        h.run(control)
        self.assertEqual(h.fast_calls(), [])

    def test_hold_timer_requires_due_full_close_hint_before_waking(self):
        h = self.h
        row = self.f.store.account("test")
        self.f.store.put("cycle:test", {"phase": "holding", "opened_at": WALL - 59,
            "quantities": {"LONG": "1", "SHORT": "1"}, "config": row["cycle"]})

        def control(step):
            if step == 2:
                h.emit(100.5)
            elif step == 3:
                h.emit(101)
            elif step == 4:
                h.engine.shutdown.set()

        h.run(control)
        self.assertEqual(len(h.fast_calls()), 1)
        self.assertEqual(h.fast_calls()[0]["at"], 101)
        self.assertEqual(h.fast_calls()[0]["signal"]["phase"], "close")

    def test_shutdown_rejects_a_late_callback_and_detaches_both_listeners(self):
        h = self.h

        def control(step):
            if step == 2:
                h.engine.shutdown.set()
                h.emit(101)

        h.run(control)
        self.assertEqual(h.fast_calls(), [])
        self.assertEqual(h.engine.cycle_market_updates, {})
        self.assertEqual(h.listeners, {"bbo": None, "depth": None})
        h.quote.close.assert_called_once_with()
        h.depth.close.assert_called_once_with()
        h.api.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
