"""Availability periods and durable dispatch timing, with no live requests."""
import json
import unittest
from urllib.parse import parse_qs
from unittest.mock import patch

import httpx

from tests.helpers import Fixture
from trading.capacity_timing import initial_timing, observe_capacity_submit, capacity_timing_text
from trading.engine import Engine, snapshot_json
from trading.exchange import API, BudgetWait, LiveBroker, RateBudget
from trading.execution import Executor
from trading.models import dec
from trading.request_timing import transport_stage
from trading.store import Store

SYMBOL = "XAUUSD1"
WALL = 1_800_000_000


class AvailabilityPeriodTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker
        self.tick = 100.0
        for name in ("trading.engine.time.monotonic", "trading.engine.time.time"):
            value = (lambda: self.tick) if name.endswith("monotonic") else (lambda: WALL + self.tick)
            mocked = patch(name, side_effect=value)
            mocked.start()
            self.addCleanup(mocked.stop)

    def sample(self, values, *, sampled=None, stamps=None, checked=None, owners=None):
        checked = WALL + self.tick if checked is None else checked
        owners = self.f.store.accounts() if owners is None else owners
        self.engine.wake_capacity_accounts(SYMBOL, {k: dec(v) for k, v in values.items()}, checked, owners,
            sampled_tiers=sampled, capacity_checked_at=stamps)

    def observation(self, leverage=5):
        return self.engine.ordinary_capacity_observation(self.f.store.account("test"), SYMBOL, leverage)

    def test_first_detection_is_response_time_and_not_refreshed_by_repeated_samples(self):
        self.sample({5: 500000}, checked=WALL + 98)
        self.assertEqual(self.observation()["detected_at"], WALL + 100)
        self.tick = 100.2
        self.sample({5: 400000})
        self.assertEqual(self.observation()["detected_at"], WALL + 100)
        self.assertEqual(self.observation()["checked_at"], WALL + 100.2)

    def test_drop_failure_and_expiry_start_new_periods(self):
        for broken in ("drop", "failed", "expired"):
            with self.subTest(broken=broken):
                self.engine.work("test").capacity_available.clear()
                self.tick = 100
                self.sample({5: 500000})
                self.tick = 101
                if broken == "drop":
                    self.sample({5: 10000})
                elif broken == "failed":
                    self.sample({}, owners=[])
                else:
                    self.tick = 109
                self.assertIsNone(self.observation())
                self.tick += .2
                self.sample({5: 500000})
                self.assertEqual(self.observation()["detected_at"], WALL + self.tick)

    def test_partial_5x_preserves_high_origin_but_cannot_extend_high_freshness(self):
        self.sample({5: 500000, 10: 500000})
        self.tick = 100.2
        self.sample({5: 500000, 10: 500000}, sampled={5}, stamps={"5": WALL + self.tick, "10": WALL + 100})
        self.assertEqual(self.observation(10)["detected_at"], WALL + 100)
        self.tick = 109
        self.sample({5: 500000, 10: 500000}, sampled={5}, stamps={"5": WALL + self.tick, "10": WALL + 100})
        self.assertIsNone(self.observation(10))
        self.assertEqual(self.observation(5)["detected_at"], WALL + 109)

    def test_final_leverage_uses_its_own_detection_period(self):
        self.sample({5: 500000})
        self.tick = 101
        self.sample({5: 500000, 10: 500000})
        self.assertEqual(self.observation(5)["detected_at"], WALL + 100)
        self.assertEqual(self.observation(10)["detected_at"], WALL + 101)
        self.assertIsNone(self.observation(20))

    def test_threshold_selection_and_pause_do_not_reuse_old_period(self):
        self.sample({5: 500000})
        owner = self.f.store.account("test")
        owner["policy"]["threshold"] = "20000"
        self.f.store.save_account(owner)
        self.assertIsNone(self.observation())
        self.tick = 101
        self.sample({5: 500000})
        self.assertEqual(self.observation()["detected_at"], WALL + 101)
        self.engine.enable("test", False)
        self.assertIsNone(self.observation())
        self.engine.enable("test", True)
        self.assertIsNone(self.observation())
        self.tick = 102
        self.sample({5: 500000})
        owner = self.f.store.account("test")
        owner["policy"]["symbols"] = [SYMBOL, "CLUSD1"]
        owner["policy"]["ordinary_symbol"] = "CLUSD1"
        self.f.store.save_account(owner)
        self.assertIsNone(self.observation())

    def publish(self):
        with patch.object(self.f.market, "capacities", return_value={5: dec(500000)}):
            self.engine.poll_market(SYMBOL)

    def test_successive_batches_keep_origin_and_completed_history_survives_restart(self):
        self.publish()
        self.tick = 100.4
        self.engine.tick_account("test")
        self.tick = 100.6
        self.publish()
        self.tick = 101.2
        self.engine.tick_account("test")
        restored = Store(self.f.store.path)
        fills = [event["message"] for event in restored.events() if event["kind"] == "fill"]
        self.assertEqual(len(fills), 2)
        self.assertIn("1200 ms", fills[0])
        self.assertIn("400 ms", fills[1])
        with restored.connect() as db:
            intents = [json.loads(row[0]) for row in db.execute("SELECT data FROM intents ORDER BY rowid")]
        self.assertEqual([i["capacity_timing"]["detected_at"] for i in intents], [WALL + 100] * 2)
        self.assertNotIn("detected_tick", json.dumps(intents))

    def test_unresolved_batch_recovery_keeps_original_timing_without_new_samples(self):
        self.publish()
        self.tick = 100.7
        with patch("trading.execution.Executor.reconcile", return_value="pending"):
            self.engine.tick_account("test")
        pending = self.f.store.intent("test")
        original = pending["capacity_timing"].copy()
        self.tick = 120
        restored = Store(self.f.store.path)
        Executor(restored, self.f.broker, self.f.market).reconcile(restored.account("test"), restored.intent("test"))
        fills = [e for e in restored.events() if e["kind"] == "fill"]
        self.assertIn("700 ms", fills[0]["message"])
        with restored.connect() as db:
            saved = json.loads(db.execute("SELECT data FROM intents WHERE id=?", (pending["id"],)).fetchone()[0])
        self.assertEqual(saved["capacity_timing"], original)

    def test_exchange_rejection_keeps_attempt_timing_without_a_fill_event(self):
        self.publish()
        self.tick = 100.8
        with patch.object(self.f.broker, "submit", return_value=[{"code": -5018, "msg": "quota"}] * 2):
            self.engine.tick_account("test")
        events = self.f.store.events()
        self.assertFalse(any(e["kind"] == "fill" for e in events))
        self.assertTrue(any("800 ms" in e["message"] for e in events))

    def test_single_leg_repair_cannot_overwrite_the_original_dispatch_measurement(self):
        self.publish()
        self.tick = 100.4
        submit = self.f.broker.submit
        calls = []
        def partial(orders):
            calls.append(orders)
            if len(orders) == 2:
                filled = submit(orders[:1])
                return [*filled, {"code": -5018, "msg": "quota"}]
            self.tick += 2
            return submit(orders)
        with patch.object(self.f.broker, "submit", side_effect=partial):
            self.engine.tick_account("test")
        self.assertEqual(len(calls), 2)
        with self.f.store.connect() as db:
            intent = json.loads(db.execute("SELECT data FROM intents ORDER BY rowid DESC LIMIT 1").fetchone()[0])
        self.assertEqual(intent["capacity_timing"]["latency_ms"], 400)
        self.assertEqual(intent["status"], "aborted")

    def test_live_http_adapter_persists_send_latency_not_response_time(self):
        calls = []
        def handle(request):
            calls.append(request.url.path)
            orders = json.loads(parse_qs(request.content.decode())["batchOrders"][0])
            self.tick += 2
            return httpx.Response(200, json=self.f.broker.submit(orders))
        api = API(transport=httpx.MockTransport(handle), budget=RateBudget())
        self.addCleanup(api.close)
        api.signed_parameters = lambda params: params
        broker = LiveBroker({}, self.f.market, api)
        self.engine.brokers["test"] = broker
        self.publish()
        self.tick = 100.5
        with patch.object(broker, "snapshot", side_effect=self.f.broker.snapshot), \
             patch("trading.request_timing.time", side_effect=lambda: WALL + self.tick), \
             patch("trading.request_timing.perf_counter", side_effect=lambda: self.tick):
            self.engine.tick_account("test")
        self.assertEqual(calls, ["/fapi/v3/batchOrders"])
        fills = [e for e in self.f.store.events() if e["kind"] == "fill"]
        self.assertIn("额度达标 → 发单：500 ms", fills[0]["message"])
        self.assertNotIn("模拟", fills[0]["message"])

    def test_threshold_observations_are_isolated_between_accounts(self):
        other = self.f.store.account("test")
        other.update(id="other", env_prefix="ASTER_OTHER")
        other["policy"]["threshold"] = "20000"
        self.f.store.save_account(other)
        self.sample({5: 15000})
        self.assertIsNone(self.engine.ordinary_capacity_observation(other, SYMBOL, 5))
        self.tick = 100.2
        self.sample({5: 30000})
        self.assertEqual(self.observation()["detected_at"], WALL + 100)
        self.assertEqual(self.engine.ordinary_capacity_observation(other, SYMBOL, 5)["detected_at"], WALL + 100.2)

    def test_persistence_wait_rechecks_period_at_submission(self):
        for changed in ("expired", "dropped", "recovered"):
            with self.subTest(changed=changed):
                self.engine.work("test").capacity_available.clear()
                self.tick = 100
                self.publish()
                self.tick = 100.4
                save, changed_once = self.f.store.save_intent, []
                def persist(intent):
                    save(intent)
                    if not changed_once:
                        changed_once.append(True)
                        self.tick = 109 if changed == "expired" else 100.5
                        if changed != "expired":
                            self.sample({5: 0})
                        if changed == "recovered":
                            self.tick = 100.6
                            self.sample({5: 500000})
                            self.tick = 100.8
                with patch.object(self.f.store, "save_intent", side_effect=persist):
                    self.engine.tick_account("test")
                with self.f.store.connect() as db:
                    intent = json.loads(db.execute("SELECT data FROM intents ORDER BY rowid DESC LIMIT 1").fetchone()[0])
                timing = intent["capacity_timing"]
                if changed == "recovered":
                    self.assertEqual(timing["detected_at"], WALL + 100.6)
                    self.assertEqual(timing["latency_ms"], 200)
                else:
                    self.assertEqual(timing["status"], "unrecorded")
                    self.assertIsNone(timing["latency_ms"])


class DispatchMeasurementTests(unittest.TestCase):
    def test_http_entry_includes_signing_but_excludes_slow_response(self):
        clock = [100.5]
        observation = {"detected_at": WALL + 100, "detected_tick": 100, "threshold": "10000"}
        timing = initial_timing(SYMBOL, 5, observation)
        with patch("trading.capacity_timing.time.monotonic", side_effect=lambda: clock[0]), \
             patch("trading.capacity_timing.time.time", side_effect=lambda: WALL + clock[0]), \
             patch("trading.request_timing.time", side_effect=lambda: WALL + clock[0]), \
             patch("trading.request_timing.perf_counter", side_effect=lambda: clock[0]):
            with observe_capacity_submit(timing, observation, live=True):
                clock[0] += .003  # signing/local setup
                with transport_stage("http_ms"):
                    clock[0] += 2  # response must not count toward dispatch
        self.assertEqual(timing["latency_ms"], 503)
        self.assertEqual(timing["status"], "measured")

    def test_local_denial_and_missing_observation_never_display_zero_latency(self):
        observation = {"detected_at": WALL, "detected_tick": 100}
        timing = initial_timing(SYMBOL, 5, observation)
        with self.assertRaises(BudgetWait), observe_capacity_submit(timing, observation, live=True):
            raise BudgetWait("local budget", retry_after=20)
        self.assertIsNone(timing["latency_ms"])
        self.assertIn("本地未发送", capacity_timing_text({"capacity_timing": timing}))
        timing = initial_timing(SYMBOL, 5, None)
        with observe_capacity_submit(timing, None, live=False):
            pass
        self.assertIsNone(timing["latency_ms"])
        self.assertIn("未记录", capacity_timing_text({"capacity_timing": timing}))
        self.assertIn("未记录", capacity_timing_text({}))
        for invalid in ("old", [], {"status": "measured", "latency_ms": float("nan")}):
            self.assertIn("未记录", capacity_timing_text({"capacity_timing": invalid}))


if __name__ == "__main__":
    unittest.main()
