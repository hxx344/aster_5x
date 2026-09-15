"""Read-only cost reporting against durable fills and real paper cycles."""
from copy import deepcopy
from fractions import Fraction
from unittest import TestCase
from unittest.mock import patch

from tests import test_cycle_daily_limit as daily_cases, test_cycle_volume as volume_cases
from tests.helpers import account
from trading.engine import Engine
from trading.models import dec
from trading.store import Store


class CycleCostStateTests(TestCase):
    setUp = daily_cases.DailyQuotaEngineTests.setUp
    select = daily_cases.DailyQuotaEngineTests.select
    expire_hold = daily_cases.DailyQuotaEngineTests.expire_hold
    run_until_holding = daily_cases.DailyQuotaEngineTests.run_until_holding
    fill = volume_cases.CycleVolumeTests.fill

    def intent(self, **changes):
        self.store = self.f.store
        return volume_cases.CycleVolumeTests.intent(self, account_id="test", **changes)

    def state_at(self, now):
        with patch("trading.engine.time.time", return_value=now):
            return self.engine.state()["accounts"][0]

    def test_completed_paper_round_reports_four_fill_fees_and_both_spreads_after_restart(self):
        now = volume_cases.DAY + 3600
        self.select(daily_volume_limit="1000", hold_seconds=1)
        self.engine.enable("test", True)
        with patch("trading.engine.time.time", return_value=now):
            self.run_until_holding()
            self.expire_hold()
            self.engine.tick_account("test")
        before = deepcopy(self.f.broker.state)
        rows = self.f.store.cycle_trade_records("test")
        self.assertEqual(len(rows), 4)
        fee = sum((Fraction(row["notional"]) for row in rows), Fraction(0)) / 8000
        spread = sum((Fraction(row["notional"]) * (1 if row["side"] == "BUY" else -1)
                      for row in rows), Fraction(0))
        self.assertGreater(spread, 0)
        current = self.state_at(now + 1)
        for window in ("daily_volume", "rolling_volume"):
            cost = current["cycle_state"][window]["cost"]
            self.assertEqual(Fraction(cost["taker_fee"]), fee)
            self.assertEqual(Fraction(cost["spread_cost"]), spread)
            self.assertEqual(Fraction(cost["total_cost"]), fee + spread)
            self.assertEqual(cost["taker_rate_percent"], "0.0125")
            self.assertTrue(cost["complete"])
            self.assertFalse(cost["sync_pending"])
        self.assertEqual(sum((Fraction(row["cost"]["total_cost"]) for row in current["cycle_trades"]), Fraction(0)), fee + spread)
        self.assertTrue(all(row["cost"]["cost_complete"] for row in current["cycle_trades"]))
        self.engine = Engine(Store(self.f.store.path), market=self.f.market)
        restored = self.state_at(now + 1)
        self.assertEqual(current["cycle_trades"], restored["cycle_trades"])
        self.assertEqual(current["cycle_state"]["daily_volume"]["cost"], restored["cycle_state"]["daily_volume"]["cost"])
        self.assertEqual(before, self.f.broker.state)

    def test_midnight_recognizes_spread_once_on_the_later_fill_day(self):
        midnight = volume_cases.DAY + 86400
        intent = self.intent(quantity="2", filled="2", short_filled="2")
        self.f.store.record_cycle_fills(intent, [
            self.fill(intent, "buy", quantity="2", price="101", executed_at=midnight - 1),
            self.fill(intent, "sell", quantity="2", price="100", position_side="SHORT", executed_at=midnight + 1),
        ])
        self.f.store.mark_cycle_volume_synced(intent["id"])
        before = self.state_at(midnight - .5)["cycle_state"]["daily_volume"]["cost"]
        self.assertEqual(before["taker_fee"], "0.02525")
        self.assertEqual(before["spread_cost"], "0")
        self.assertFalse(before["complete"])
        after = self.state_at(midnight + 2)
        daily = after["cycle_state"]["daily_volume"]["cost"]
        rolling = after["cycle_state"]["rolling_volume"]["cost"]
        self.assertEqual((daily["taker_fee"], daily["spread_cost"], daily["total_cost"]), ("0.025", "2", "2.025"))
        self.assertEqual(rolling["total_cost"], "2.05025")
        self.assertTrue(daily["complete"])
        by_id = {row["trade_id"]: row["cost"] for row in after["cycle_trades"]}
        self.assertEqual((by_id["buy"]["spread_cost"], by_id["sell"]["spread_cost"]), ("0", "2"))
        released = self.state_at(midnight + 86401)["cycle_state"]["rolling_volume"]["cost"]
        self.assertEqual(released["total_cost"], "0")

    def test_active_batch_without_error_and_terminal_backlog_never_claim_complete_cost(self):
        intent = self.intent(status="pending", quantity="2", filled="2", short_filled="2", completed_at=None)
        self.f.store.record_cycle_fills(intent, [
            self.fill(intent, "buy"), self.fill(intent, "sell", position_side="SHORT"),
        ])
        current = self.state_at(volume_cases.DAY + 100)
        cost = current["cycle_state"]["daily_volume"]["cost"]
        self.assertTrue(cost["sync_pending"])
        self.assertFalse(cost["complete"])
        self.assertEqual(cost["total_cost"], "0.025")
        intent.update(status="complete", completed_at=volume_cases.DAY + 30)
        self.f.store.save_intent(intent)
        cost = self.state_at(volume_cases.DAY + 100)["cycle_state"]["rolling_volume"]["cost"]
        self.assertTrue(cost["sync_pending"])
        self.assertFalse(cost["complete"])
        self.f.store.record_cycle_fills(intent, [
            self.fill(intent, "buy2"), self.fill(intent, "sell2", position_side="SHORT"),
        ])
        self.f.store.mark_cycle_volume_synced(intent["id"])
        cost = self.state_at(volume_cases.DAY + 100)["cycle_state"]["rolling_volume"]["cost"]
        self.assertFalse(cost["sync_pending"])
        self.assertTrue(cost["complete"])
        self.assertEqual(cost["total_cost"], "0.05")

    def test_report_failure_is_unknown_and_does_not_block_closing_or_other_account(self):
        self.select(hold_seconds=1)
        self.engine.enable("test", True)
        self.run_until_holding()
        self.f.store.save_account(account("second"))
        original = self.f.store.cycle_cost_records
        def read(aid, **kwargs):
            if aid == "test":
                raise RuntimeError("cost query failed")
            return original(aid, **kwargs)
        with patch.object(self.f.store, "cycle_cost_records", side_effect=read):
            self.expire_hold()
            self.engine.tick_account("test")
            self.assertEqual(self.f.store.get("cycle:test")["completed_cycles"], 1)
            with self.assertLogs("aster.trading", level="WARNING"):
                result = {row["id"]: row for row in self.engine.state()["accounts"]}
        current = result["test"]
        self.assertGreater(dec(current["cycle_state"]["daily_volume"]["volume"]), 0)
        self.assertTrue(current["enabled"])
        cost = current["cycle_state"]["daily_volume"]["cost"]
        self.assertIsNone(cost["total_cost"])
        self.assertFalse(cost["complete"])
        self.assertIn("暂不可用", cost["error"])
        self.assertTrue(all("cost" not in row for row in current["cycle_trades"]))
        other = result["second"]["cycle_state"]["daily_volume"]["cost"]
        self.assertEqual(other["total_cost"], "0")
        self.assertTrue(other["complete"])

    def test_calculation_failure_keeps_authenticated_state_response_available(self):
        from fastapi.testclient import TestClient
        from trading.server import create_app
        with patch.dict("os.environ", {"ASTER_DASHBOARD_PASSWORD": "test-only-cost-password"}, clear=True):
            client = TestClient(create_app(self.engine, start_engine=False))
            self.addCleanup(client.close)
            client.headers["origin"] = "http://testserver"
            self.assertEqual(client.post("/api/login", json={"password": "test-only-cost-password"}).status_code, 200)
            with patch("trading.engine.calculate_cycle_costs", side_effect=ValueError("invalid ledger row")), \
                 self.assertLogs("aster.trading", level="WARNING"):
                response = client.get("/api/state")
                self.assertEqual(response.status_code, 200)
                self.assertNotIn("daily_volume", response.json()["accounts"][0]["cycle_state"])
                self.engine.dashboard_reports.worker.join(timeout=5)
                self.assertFalse(self.engine.dashboard_reports.worker.is_alive())
                response = client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["accounts"][0]["cycle_state"]["daily_volume"]["cost"]["taker_fee"])
