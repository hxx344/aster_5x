"""Private order events replace only CID queries, never post-fill risk reads."""
import json
import time
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_warm_mode_recovery as warm_fixtures
from tests import test_cycle_hot_engine as engine_fixtures
from trading.exchange import AmbiguousOrder
from trading.models import dec
from trading.user_stream import PrivateAccountStream

SYMBOL = "XAUUSD1"


class PairWsReconciliationTests(TestCase):
    live_brokers = warm_fixtures.PairWarmModeRecoveryTests.live_brokers
    setUp = warm_fixtures.PairWarmModeRecoveryTests.setUp
    warm = warm_fixtures.PairWarmModeRecoveryTests.warm
    clear_calls = warm_fixtures.PairWarmModeRecoveryTests.clear_calls
    paths = warm_fixtures.PairWarmModeRecoveryTests.paths
    ordinary_plan = warm_fixtures.PairWarmModeRecoveryTests.ordinary_plan

    def connect(self):
        with patch.object(PrivateAccountStream, "start"):
            for broker in self.brokers.values():
                broker.start_cycle_hot_data([SYMBOL])
                broker.cycle_stream._set_connected(True)
                self.addCleanup(broker.stop_cycle_hot_data)

    def run_with_events(self, missing=None, partial=False, disconnect=False):
        self.connect()
        state, snapshots, guards, plan = self.ordinary_plan()
        for key, broker in self.brokers.items():
            original = broker.submit

            def submit(orders, *, timeout=None, key=key, broker=broker, original=original):
                receipt = original(orders, timeout=timeout)[0]
                order = orders[0]
                if key != missing:
                    stamp = int(time.time() * 1000)
                    event = {"e": "ORDER_TRADE_UPDATE", "E": stamp, "T": stamp,
                        "o": {"s": SYMBOL, "c": order["newClientOrderId"], "S": order["side"],
                            "ps": order["positionSide"], "o": "MARKET", "ot": "MARKET",
                            "q": order["quantity"], "i": 123 if key == "long" else 456,
                            "X": "PARTIALLY_FILLED" if partial else "FILLED", "x": "TRADE",
                            "z": str(dec(receipt["executedQty"]) / 2) if partial else receipt["executedQty"],
                            "ap": receipt["avgPrice"], "T": stamp}}
                    broker.cycle_stream._handle_message(json.dumps(event))
                    if disconnect:
                        broker.cycle_stream._set_connected(False)
                raise AmbiguousOrder("Offline POST response lost")

            patcher = patch.object(broker, "submit", side_effect=submit)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.trader._start(self.pair, state, self.brokers, snapshots, guards, plan, kind="ordinary")
        self.assertIsNone(state["pending"], state)
        self.assertTrue(state["last_batch"]["completed"])
        self.assertTrue(all(dec(qty) == plan.qty for qty in state["owned"].values()))
        for key in self.brokers:
            paths = self.paths(key)
            self.assertIn("/fapi/v3/accountWithJoinMargin", paths)
            self.assertIn("/fapi/v3/positionRisk", paths)
            self.assertEqual(sum(method == "POST" for method, _, _ in self.brokers[key].api.calls), 1)
        return state

    def order_gets(self, key):
        return sum(method == "GET" and path == "/fapi/v3/order"
                   for method, path, _ in self.brokers[key].api.calls)

    def test_terminal_events_finish_lost_http_responses_without_order_gets(self):
        self.run_with_events()
        self.assertEqual([self.order_gets(key) for key in self.brokers], [0, 0])

    def test_only_missing_side_uses_original_cid_rest(self):
        self.run_with_events(missing="short")
        self.assertEqual(self.order_gets("long"), 0)
        self.assertEqual(self.order_gets("short"), 1)

    def test_partial_events_do_not_hide_later_rest_terminal_receipts(self):
        self.run_with_events(partial=True)
        self.assertEqual([self.order_gets(key) for key in self.brokers], [1, 1])

    def test_disconnected_session_cannot_supply_old_terminal_evidence(self):
        self.run_with_events(disconnect=True)
        self.assertEqual([self.order_gets(key) for key in self.brokers], [1, 1])


class PausedTransferStreamTests(TestCase):
    setUp = engine_fixtures.CycleHotEngineTests.setUp
    warm = engine_fixtures.CycleHotEngineTests.warm
    publish = engine_fixtures.CycleHotEngineTests.publish

    def test_only_pending_transfer_keeps_paused_group_stream_without_rest(self):
        row = self.f.store.account("test")
        row.update(enabled=False, cycle={**row["cycle"], "enabled": False})
        self.f.store.save_account(row)
        pair = {"id": "gold", "cycle": {"enabled": False}, "enabled": False}
        self.f.store.put("pair_margin:gold", {"pending": {"status": "acknowledged"}})
        with patch.object(self.engine.pairs, "active_for_account", return_value=None), \
             patch.object(self.f.store, "pair_for_account", return_value=pair), \
             patch.object(self.broker, "stop_cycle_hot_data") as stop:
            self.assertEqual(self.engine.poll_cycle_hot_data("test"), 30)
            self.broker.start_cycle_hot_data.assert_called_once()
            stop.assert_not_called()
            self.broker.refresh_cycle_hot_snapshot.assert_not_called()
            self.assertFalse(self.f.store.account("test")["enabled"])
            self.f.store.put("pair_margin:gold", {})
            self.engine.poll_cycle_hot_data("test")
            stop.assert_called_once()
        self.api.call.assert_not_called()
