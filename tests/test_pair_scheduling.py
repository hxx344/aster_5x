"""Exercise paired controls through the real scheduler with offline futures."""
from unittest import TestCase
from unittest.mock import patch

from tests.helpers import Fixture, account
from tests.test_cycle_ws_scheduler import _Harness, _Future
from trading.pairing import validate_pair


class PairSchedulingTests(TestCase):
    def test_pause_during_worker_wakes_after_completion_despite_long_backoff(self):
        fixture = Fixture()
        self.addCleanup(fixture.close)
        harness = _Harness(fixture)
        for aid in ("test", "second"):
            fixture.store.save_account({**account(aid), "enabled": False})
        fixture.store.save_pair(validate_pair({"id": "gold", "name": "黄金",
            "long_account_id": "test", "short_account_id": "second",
            "enabled": True, "cycle": {"enabled": True}}), create=True)
        held, calls = _Future(60, done=False), []

        def tick(pair_id):
            if calls:
                self.assertTrue(held.done(), "one group must never have concurrent workers")
            calls.append((harness.ticks, fixture.store.pair(pair_id)["enabled"]))
            return held if len(calls) == 1 else _Future(60)

        def control(step):
            if step == 1:
                harness.ticks = 101
                harness.engine.pairs.enable("gold", False)
            elif step == 2:
                harness.ticks = 102
            elif step == 3:
                harness.ticks = 103
                held.complete = True
            elif step == 4:
                harness.engine.shutdown.set()

        with patch.object(harness.engine.pairs, "tick", side_effect=tick):
            harness.run(control)
        self.assertEqual(calls, [(100, True), (103, False)])
