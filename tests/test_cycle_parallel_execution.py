"""Same-account cycle ownership remains local to its selected market."""
from copy import deepcopy
from fractions import Fraction
from unittest import TestCase
from unittest.mock import Mock, patch

from tests import test_cycle_execution as execution_cases
from .helpers import Fixture
from trading.cycle import CyclePositionError, DEFAULT_CYCLE, validate_cycle_positions
from trading.cycle_execution import CycleExecutor
from trading.exchange import LiveBroker
from trading.execution import Executor
from trading.models import AccountModeError, SYMBOLS, TradingError, dec
from trading.paper import PAPER_BRACKETS


CYCLE_SYMBOL = "XAUUSD1"


class CycleParallelExecutionTests(TestCase):
    progress_now = execution_cases.CycleExecutionTests.progress_now
    plan = execution_cases.CycleExecutionTests.plan
    open = execution_cases.CycleExecutionTests.open
    close_cycle = execution_cases.CycleExecutionTests.close_cycle
    quantities = execution_cases.CycleExecutionTests.quantities

    def setUp(self):
        execution_cases.CycleExecutionTests.setUp(self)
        self.f.account["policy"]["symbols"] = list(SYMBOLS)
        self.f.store.save_account(self.f.account)
        self.ordinary = Executor(self.f.store, self.f.broker, self.f.market)

    def ordinary_plan(self, symbol):
        from trading.models import plan_pair

        snapshot = self.f.broker.snapshot(self.f.account["policy"]["symbols"])
        book = self.f.market.book(symbol)
        plan = plan_pair(snapshot, book, self.f.market.rules[symbol], {5: dec(500000)}, self.f.account["policy"])
        self.assertGreater(plan.qty, 0)
        return snapshot, plan, book

    def open_ordinary(self, symbol):
        snapshot, plan, book = self.ordinary_plan(symbol)
        return self.ordinary.open_pair(self.f.account, snapshot, symbol, plan, book)

    def ordinary_positions(self):
        return {key: deepcopy(value) for key, value in self.f.broker.state["positions"].items()
                if not key.startswith(CYCLE_SYMBOL + ":")}

    def test_other_ordinary_positions_survive_cycle_and_stay_out_of_volume_ledger(self):
        self.open_ordinary("SPCXUSD1")
        self.open_ordinary("CLUSD1")
        ordinary_positions = self.ordinary_positions()
        self.assertTrue(all(dec(row["qty"]) > 0 for row in ordinary_positions.values()))
        self.assertEqual(self.f.store.cycle_trade_records("test"), [])
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.open()
            self.close_cycle()
        self.assertEqual(submit.call_count, 2)
        self.assertTrue(all(order["symbol"] == CYCLE_SYMBOL for call in submit.call_args_list for order in call.args[0]))
        self.assertEqual(self.quantities(), (0, 0))
        self.assertEqual(self.ordinary_positions(), ordinary_positions)
        self.assertEqual(self.progress_now()["completed_cycles"], 1)
        trades = self.f.store.cycle_trade_records("test")
        self.assertEqual(len(trades), 4)
        self.assertEqual({row["symbol"] for row in trades}, {CYCLE_SYMBOL})
        self.assertEqual({row["phase"] for row in trades}, {"open", "close"})
        now = max(row["executed_at"] for row in trades) + 1
        daily = self.f.store.cycle_daily_volume("test", now=now)
        expected_day = sum((Fraction(dec(row["notional"])) for row in trades if row["utc_date"] == daily["utc_date"]), Fraction(0))
        self.assertEqual(Fraction(dec(daily["volume"])), expected_day)
        self.assertEqual(Fraction(dec(self.f.store.cycle_rolling_volume("test", now=now)["volume"])),
                         sum((Fraction(dec(row["notional"])) for row in trades), Fraction(0)))

    def test_ordinary_market_can_add_during_cycle_holding_and_survives_cycle_close(self):
        self.open()
        self.assertEqual(self.progress_now()["phase"], "holding")
        self.open_ordinary("SPCXUSD1")
        ordinary_positions = self.ordinary_positions()
        self.assertGreater(dec(ordinary_positions["SPCXUSD1:LONG"]["qty"]), 0)
        self.assertEqual(self.quantities(), (2, 2))
        self.close_cycle()
        self.assertEqual(self.quantities(), (0, 0))
        self.assertEqual(self.ordinary_positions(), ordinary_positions)
        self.assertEqual({row["symbol"] for row in self.f.store.cycle_trade_records("test")}, {CYCLE_SYMBOL})

    def test_partial_cycle_repair_reduces_no_other_market_positions(self):
        self.open_ordinary("SPCXUSD1")
        self.open_ordinary("CLUSD1")
        ordinary_positions = self.ordinary_positions()
        original = self.f.broker.submit

        def partial(orders):
            return [original(orders[:1])[0], {"code": -2019}] if len(orders) == 2 else original(orders)

        with patch.object(self.f.broker, "submit", side_effect=partial) as submit:
            self.open()
        self.assertEqual(submit.call_count, 2)
        self.assertEqual([(order["symbol"], order["positionSide"], order["side"], order["quantity"])
                          for order in submit.call_args_list[1].args[0]], [(CYCLE_SYMBOL, "LONG", "SELL", "2")])
        self.assertEqual(self.quantities(), (0, 0))
        self.assertEqual(self.ordinary_positions(), ordinary_positions)
        self.assertIsNone(self.f.store.intent("test"))

    def test_ordinary_pending_blocks_cycle_until_existing_receipts_are_reconciled(self):
        with patch.object(self.ordinary, "reconcile", return_value="interrupted before receipt reconciliation"):
            self.open_ordinary("SPCXUSD1")
        pending = self.f.store.intent("test")
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            with self.assertRaisesRegex(TradingError, "已有批次正在执行"):
                self.open()
            self.assertEqual(self.f.store.intent("test")["id"], pending["id"])
            self.ordinary.reconcile(self.f.account)
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))
        ordinary_positions = self.ordinary_positions()
        self.open()
        self.assertEqual(self.quantities(), (2, 2))
        self.assertEqual(self.ordinary_positions(), ordinary_positions)

    def test_cycle_pending_blocks_ordinary_until_existing_cycle_reconciles(self):
        with patch.object(self.executor, "reconcile", return_value="interrupted before receipt reconciliation"):
            self.open()
        pending = self.f.store.intent("test")
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            with self.assertRaisesRegex(TradingError, "已有批次正在执行"):
                self.open_ordinary("SPCXUSD1")
            self.assertEqual(self.f.store.intent("test")["id"], pending["id"])
            CycleExecutor(self.f.store, self.f.broker, self.f.market).reconcile(self.f.account)
        submit.assert_not_called()
        self.assertIsNone(self.f.store.intent("test"))
        self.assertEqual(self.quantities(), (2, 2))
        self.open_ordinary("SPCXUSD1")
        self.assertGreater(dec(self.ordinary_positions()["SPCXUSD1:LONG"]["qty"]), 0)

    def test_selected_market_untracked_position_is_still_rejected(self):
        self.open_ordinary("SPCXUSD1")
        ordinary_positions = self.ordinary_positions()
        self.f.broker.state["positions"][CYCLE_SYMBOL + ":LONG"] = {"qty": "1", "entry": "4412"}
        self.f.broker.save()
        with self.assertRaisesRegex(CyclePositionError, "循环品种已有仓位"):
            validate_cycle_positions(self.f.account, self.f.broker.cycle_snapshot([CYCLE_SYMBOL]), self.progress_now())
        self.assertEqual(self.ordinary_positions(), ordinary_positions)

    def test_ordinary_upgrades_cannot_change_selected_cycle_leverage(self):
        with patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as mutate, \
             patch.object(self.f.store, "save_intent", wraps=self.f.store.save_intent) as save_intent:
            for target in (5, 10, 20):
                with self.subTest(target=target), self.assertRaisesRegex(TradingError, "循环"):
                    self.ordinary.leverage(self.f.account, CYCLE_SYMBOL, 2, target)
        mutate.assert_not_called()
        save_intent.assert_not_called()
        self.assertEqual(self.f.broker.state["leverages"][CYCLE_SYMBOL], 2)

    def test_ordinary_upgrade_rechecks_saved_cycle_after_final_callback(self):
        stale = deepcopy(self.f.account)
        stale["cycle"]["enabled"] = False
        self.f.store.save_account(stale)

        def enable_cycle(_snapshot):
            self.f.store.save_account(self.f.account)

        with patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as mutate, \
             patch.object(self.f.store, "save_intent", wraps=self.f.store.save_intent) as save_intent:
            with self.assertRaisesRegex(TradingError, "循环"):
                self.ordinary.leverage(stale, CYCLE_SYMBOL, 2, 5, before_submit=enable_cycle)
        mutate.assert_not_called()
        save_intent.assert_not_called()
        self.assertTrue(self.f.store.account("test")["cycle"]["enabled"])
        self.assertEqual(self.f.broker.state["leverages"][CYCLE_SYMBOL], 2)

    def test_other_market_can_upgrade_while_cycle_holds_its_own_positions(self):
        self.open()
        self.open_ordinary("SPCXUSD1")
        progress, positions = deepcopy(self.progress_now()), self.ordinary_positions()
        with patch.object(self.f.broker, "set_leverage", wraps=self.f.broker.set_leverage) as mutate:
            self.ordinary.leverage(self.f.account, "SPCXUSD1", 5, 10)
            self.ordinary.reconcile(self.f.account)
        mutate.assert_called_once_with("SPCXUSD1", 10)
        self.assertEqual(self.f.broker.state["leverages"][CYCLE_SYMBOL], 2)
        self.assertEqual(self.f.broker.state["leverages"]["SPCXUSD1"], 10)
        self.assertEqual(self.quantities(), (2, 2))
        self.assertEqual(self.ordinary_positions(), positions)
        self.assertEqual(self.progress_now(), progress)
        self.assertIsNone(self.f.store.intent("test"))


class CycleParallelSnapshotTests(TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.account["cycle"] = {**DEFAULT_CYCLE, "enabled": True}
        self.rows = []
        for symbol, quantity, leverage in ((CYCLE_SYMBOL, "0", "2"), ("SPCXUSD1", "3", "10"), ("CLUSD1", "4", "20")):
            self.rows.extend({"symbol": symbol, "positionSide": side, "positionAmt": quantity,
                              "entryPrice": "100" if quantity != "0" else "0", "markPrice": "100",
                              "leverage": leverage, "unRealizedProfit": "0", "liquidationPrice": "0", "marginType": "cross"}
                             for side in ("LONG", "SHORT"))
        self.api = Mock()
        self.api.budget = None
        self.api.call.side_effect = self.api_call
        self.live = LiveBroker({}, self.f.market, api=self.api)

    def api_call(self, method, path, params=None, **kwargs):
        self.assertEqual(method, "GET")
        if path == "/fapi/v3/positionSide/dual":
            return {"dualSidePosition": True}
        if path == "/fapi/v3/multiAssetsMargin":
            return {"multiAssetsMargin": False}
        if path == "/fapi/v3/accountWithJoinMargin":
            return {"canTrade": True, "assets": [{"asset": "USD1", "crossWalletBalance": "25000",
                                                   "crossUnPnl": "0", "maintMargin": "0", "availableBalance": "24900"}],
                    "positions": [{**{key: row[key] for key in ("symbol", "positionSide", "positionAmt", "entryPrice", "leverage")},
                                   "unrealizedProfit": row["unRealizedProfit"], "maxNotional": "1000000",
                                   "isolated": row["marginType"] != "cross"} for row in self.rows]}
        if path == "/fapi/v3/positionRisk":
            return deepcopy(self.rows)
        if path == "/fapi/v3/leverageBracket":
            return {"symbol": params["symbol"], "brackets": PAPER_BRACKETS}
        if path == "/fapi/v3/openOrders":
            raise AssertionError("taker cycle must not query external order inventory")
        raise AssertionError("unexpected request: " + path)

    def test_cycle_snapshot_keeps_other_exposure_without_querying_orders(self):
        snapshot = self.live.cycle_snapshot([CYCLE_SYMBOL])
        self.assertEqual(snapshot.occupied_margin_exact,
                         6 * Fraction(self.f.market.book("SPCXUSD1").mark) / 10
                         + 8 * Fraction(self.f.market.book("CLUSD1").mark) / 20)
        self.assertEqual({position.symbol for position in snapshot.positions if position.qty}, {"SPCXUSD1", "CLUSD1"})
        self.assertIsNone(snapshot.open_orders)
        self.assertEqual(tuple(position.qty for position in CycleExecutor._ready(snapshot, CYCLE_SYMBOL)), (0, 0))
        validate_cycle_positions(self.f.account, snapshot)
        order_reads = [call for call in self.api.call.call_args_list if call.args[1] == "/fapi/v3/openOrders"]
        self.assertEqual(order_reads, [])

    def test_selected_external_position_still_blocks_cycle_with_other_markets_present(self):
        self.rows[0].update(positionAmt="1", entryPrice="100")
        snapshot = self.live.cycle_snapshot([CYCLE_SYMBOL])
        self.assertIsNone(snapshot.open_orders)
        with self.assertRaises(CyclePositionError):
            validate_cycle_positions(self.f.account, snapshot)

    def test_other_active_isolated_position_still_blocks_account_mode_check(self):
        self.rows[2]["marginType"] = "isolated"
        snapshot = self.live.cycle_snapshot([CYCLE_SYMBOL])
        with self.assertRaisesRegex(AccountModeError, "全仓保证金模式"):
            CycleExecutor._ready(snapshot, CYCLE_SYMBOL)
