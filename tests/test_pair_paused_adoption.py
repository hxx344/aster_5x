"""Paused baseline adoption, using only paper balances and offline GET fixtures."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from copy import deepcopy
from dataclasses import replace
import sqlite3
import threading
import time
import unittest
from unittest.mock import patch

from tests import test_pair_integration as integration
from tests.helpers import seed_cycle_capacity
from tests.test_cycle_account_snapshot import ACCOUNT, RISK, SYMBOL, risk_row
from trading.models import TradingError, dec
from trading.pair_execution import PairTrader, empty_progress
from trading.store import Store


class PairPausedAdoptionTests(unittest.TestCase):
    setUp = integration.PairIntegrationTests.setUp
    create = integration.PairIntegrationTests.create

    def seed_paused(self, owned=None, **extra):
        self.create()
        owned = owned or {"LONG": "1", "SHORT": "1"}
        runtime = {"phase": "attention", "attention": "人工调整后待核对", "owned": owned,
                   "progress": empty_progress(owned, 7), "pending": None, **extra}
        self.f.store.put("pair_runtime:gold", runtime)
        return deepcopy(runtime)

    def positions(self, long="0", short="0"):
        for aid, side, quantity in (("test", "LONG", long), ("second", "SHORT", short)):
            broker = self.engine.broker(self.f.store.account(aid))
            broker.state["positions"][SYMBOL + ":" + side].update(qty=quantity, entry="4412.015")
            broker.save()

    def event_rows(self):
        with self.f.store.connect() as db:
            return [tuple(row) for row in db.execute("SELECT * FROM events ORDER BY id")]

    def no_market_writes(self):
        stack = ExitStack()
        for aid in ("test", "second"):
            broker = self.engine.broker(self.f.store.account(aid))
            stack.enter_context(patch.object(broker, "submit", side_effect=AssertionError("activation must not trade")))
            stack.enter_context(patch.object(broker, "set_cycle_leverage", side_effect=AssertionError("activation must not set leverage")))
        return stack

    def assert_rejected_unchanged(self):
        pair = self.f.store.pair("gold")
        runtime = self.f.store.get("pair_runtime:gold")
        events = self.event_rows()
        with self.no_market_writes(), self.assertRaises(TradingError):
            self.engine.pairs.enable("gold", True)
        self.assertEqual(self.f.store.pair("gold"), pair)
        self.assertEqual(self.f.store.get("pair_runtime:gold"), runtime)
        self.assertEqual(self.event_rows(), events)

    def test_paused_start_adopts_increases_reductions_flat_and_unequal_sides(self):
        self.seed_paused()
        cases = (("2", "2"), ("0.25", "0.4"), ("0", "0"), ("0", "0.75"))
        for long, short in cases:
            with self.subTest(long=long, short=short):
                self.positions(long, short)
                with self.no_market_writes():
                    result = self.engine.pairs.enable("gold", True)
                self.assertTrue(result["enabled"])
                state = self.f.store.get("pair_runtime:gold")
                expected = {"LONG": long, "SHORT": short}
                self.assertEqual(state["owned"], expected)
                self.assertEqual(state["progress"]["baseline"], expected)
                self.assertEqual(state["progress"]["quantities"], {"LONG": "0", "SHORT": "0"})
                self.assertEqual(state["progress"]["completed_cycles"], 7)
                self.assertIsNone(state["pending"])
                self.assertNotIn("attention", state)
                self.engine.pairs.enable("gold", False)

    def test_adoption_preserves_accounting_backoff_and_completed_history(self):
        retained = {"daily_volume": {"2026-09-29": {"long": "100", "short": "200"}},
                    "volume_unknown": True, "volume_unknown_until_utc": "2026-09-29",
                    "last_batch": {"id": "previous", "completed": False},
                    "failure_count": 4, "retry_at": time.time() + 200}
        original = self.seed_paused(**retained)
        original["progress"].update(opened_at=100, config={"hold_seconds": 30})
        self.f.store.put("pair_runtime:gold", original)
        history = {"id": "previous", "kind": "ordinary", "quantity": "1", "completed": True}
        self.f.store.put("pair_batch:previous", history)
        self.positions("0.4", "0.7")
        with self.no_market_writes():
            self.engine.pairs.enable("gold", True)
        state = self.f.store.get("pair_runtime:gold")
        for name, value in retained.items():
            self.assertEqual(state[name], value, name)
        self.assertEqual(state["progress"]["completed_cycles"], 7)
        self.assertEqual(state["progress"]["baseline"], {"LONG": "0.4", "SHORT": "0.7"})
        self.assertNotIn("opened_at", state["progress"])
        self.assertNotIn("config", state["progress"])
        self.assertEqual(self.f.store.get("pair_batch:previous"), history)

    def test_cycle_close_returns_to_new_unequal_baseline(self):
        self.seed_paused()
        self.positions("0.2", "0.3")
        self.engine.pairs.enable("gold", True)
        baseline = {"LONG": "0.2", "SHORT": "0.3"}
        trader = PairTrader(self.engine)
        seed_cycle_capacity(self.engine)
        with patch("trading.margin_balance.MarginBalancer.tick", return_value={"blocks_trading": False}):
            state = trader.tick(self.f.store.pair("gold"))
            self.assertEqual(state["phase"], "holding", state)
            self.assertTrue(all(dec(value) > 0 for value in state["progress"]["quantities"].values()))
            self.engine.pairs.enable("gold", False)
            state = trader.tick(self.f.store.pair("gold"))
        self.assertIsNone(state["pending"], state)
        self.assertEqual(state["owned"], baseline)
        self.assertEqual(state["progress"]["completed_cycles"], 8)
        for aid, index, side in (("test", 0, "LONG"), ("second", 1, "SHORT")):
            snapshot = self.engine.broker(self.f.store.account(aid)).snapshot([SYMBOL])
            self.assertEqual(snapshot.pair(SYMBOL)[index].qty, dec(baseline[side]))

    def test_repeated_start_cannot_adopt_positions_while_running(self):
        self.seed_paused({"LONG": "0", "SHORT": "0"})
        self.engine.pairs.enable("gold", True)
        self.positions("0.25", "0.5")
        self.assert_rejected_unchanged()

    def test_running_external_position_change_still_blocks_trading(self):
        self.seed_paused({"LONG": "0", "SHORT": "0"})
        self.engine.pairs.enable("gold", True)
        self.positions("0.25", "0.5")
        seed_cycle_capacity(self.engine)
        with self.no_market_writes():
            state = PairTrader(self.engine).tick(self.f.store.pair("gold"))
        self.assertEqual(state["phase"], "attention", state)
        self.assertEqual(state["owned"], {"LONG": "0", "SHORT": "0"})
        self.assertIsNone(state["pending"])

    def test_pending_order_cycle_increment_and_transfer_each_block_adoption(self):
        initial = self.seed_paused()
        self.positions("0.4", "0.6")
        cases = (
            ("order", {**initial, "pending": {"id": "uncertain"}}, {}),
            ("empty_order", {**initial, "pending": {}}, {}),
            ("cycle", {**initial, "progress": {**initial["progress"], "quantities": {"LONG": "0.01", "SHORT": "0"}}}, {}),
            ("transfer", initial, {"pending": {"id": "transfer"}}),
            ("empty_transfer", initial, {"pending": {}}),
            *[(status, initial, {"status": status}) for status in ("submitting", "accepted", "acknowledged", "unknown")],
        )
        for label, runtime, margin in cases:
            with self.subTest(blocker=label):
                self.f.store.put("pair_runtime:gold", runtime)
                self.f.store.put("pair_margin:gold", margin)
                self.assert_rejected_unchanged()

    def test_opposite_and_other_symbol_positions_are_not_adopted(self):
        self.seed_paused()
        self.positions("0.4", "0.6")
        broker = self.f.broker
        for symbol, side in ((SYMBOL, "SHORT"), ("SPCXUSD1", "LONG")):
            with self.subTest(symbol=symbol, side=side):
                row = broker.state["positions"][symbol + ":" + side]
                row.update(qty="0.1", entry=str(self.f.market.book(symbol).mark))
                broker.save()
                self.assert_rejected_unchanged()
                broker.state["positions"][symbol + ":" + side]["qty"] = "0"
                broker.save()

    def test_modes_permissions_equity_orders_and_leverage_remain_required(self):
        self.seed_paused()
        self.positions("0.4", "0.6")
        broker = self.f.broker
        original = broker.snapshot
        transforms = {
            "hedge": lambda snap: replace(snap, hedge_mode=False),
            "asset_mode": lambda snap: replace(snap, multi_assets=True),
            "isolated": lambda snap: replace(snap, positions=[replace(p, isolated=True) if p.symbol == SYMBOL else p for p in snap.positions]),
            "permission": lambda snap: replace(snap, can_trade=False),
            "equity": lambda snap: replace(snap, equity=dec(0)),
            "orders": lambda snap: replace(snap, open_orders=[{"orderId": "external"}]),
            "unknown_orders": lambda snap: replace(snap, open_orders=None),
            "leverage": lambda snap: replace(snap, positions=[replace(p, leverage=10) if p.symbol == SYMBOL else p for p in snap.positions]),
        }
        for name, transform in transforms.items():
            with self.subTest(requirement=name), patch.object(broker, "snapshot", side_effect=lambda *args, **kwargs: transform(original(*args, **kwargs))):
                self.assert_rejected_unchanged()

    def test_creation_deletion_and_flat_reconciliation_keep_strict_positions(self):
        self.positions("0.1", "0")
        with self.assertRaises(TradingError):
            self.create()
        self.assertIsNone(self.f.store.pair("gold"))
        self.positions()
        self.create()
        self.positions("0.1", "0.2")
        for action in (self.engine.pairs.delete, self.engine.pairs.reconcile_flat):
            with self.subTest(action=action.__name__), self.assertRaises(TradingError):
                action("gold")
        self.assertIsNotNone(self.f.store.pair("gold"))

    def test_other_store_runtime_change_during_verification_is_not_overwritten(self):
        original = self.seed_paused({"LONG": "0", "SHORT": "0"})
        other = Store(self.f.store.path)
        injected = {**original, "pending": {"id": "new-unresolved-order"}}
        with patch("trading.margin_balance.MarginBalancer.verify_members", side_effect=lambda pair: other.put("pair_runtime:gold", injected)):
            with self.assertRaises(TradingError):
                self.engine.pairs.enable("gold", True)
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertEqual(self.f.store.get("pair_runtime:gold"), injected)

    def test_other_store_margin_change_during_verification_blocks_activation(self):
        original = self.seed_paused({"LONG": "0", "SHORT": "0"})
        other = Store(self.f.store.path)
        injected = {"status": "unknown", "pending": {"id": "new-unresolved-transfer"}}
        with patch("trading.margin_balance.MarginBalancer.verify_members", side_effect=lambda pair: other.put("pair_margin:gold", injected)):
            with self.assertRaises(TradingError):
                self.engine.pairs.enable("gold", True)
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertEqual(self.f.store.get("pair_runtime:gold"), original)
        self.assertEqual(self.f.store.get("pair_margin:gold"), injected)

    def test_other_store_member_change_during_verification_blocks_activation(self):
        original = self.seed_paused({"LONG": "0", "SHORT": "0"})
        other = Store(self.f.store.path)
        changed = {**other.account("test"), "env_prefix": "ASTER_REPLACED"}
        with patch("trading.margin_balance.MarginBalancer.verify_members", side_effect=lambda pair: other.save_account(changed)):
            with self.assertRaises(TradingError):
                self.engine.pairs.enable("gold", True)
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertEqual(self.f.store.get("pair_runtime:gold"), original)
        self.assertEqual(self.f.store.account("test")["env_prefix"], "ASTER_REPLACED")

    def test_other_store_pair_configuration_change_blocks_activation(self):
        original = self.seed_paused({"LONG": "0", "SHORT": "0"})
        other = Store(self.f.store.path)
        changed = {**other.pair("gold"), "name": "concurrent edit"}
        with patch("trading.margin_balance.MarginBalancer.verify_members", side_effect=lambda pair: other.save_pair(changed)):
            with self.assertRaises(TradingError):
                self.engine.pairs.enable("gold", True)
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertEqual(self.f.store.pair("gold")["name"], "concurrent edit")
        self.assertEqual(self.f.store.get("pair_runtime:gold"), original)

    def test_event_write_failure_rolls_back_configuration_and_baseline_together(self):
        original = self.seed_paused({"LONG": "0", "SHORT": "0"})
        self.positions("0.3", "0.5")
        pair, events = self.f.store.pair("gold"), self.event_rows()
        with self.f.store.connect() as db:
            db.execute("CREATE TRIGGER fail_adoption_event BEFORE INSERT ON events "
                       "WHEN NEW.account_id='gold' BEGIN SELECT RAISE(ABORT, 'injected adoption fault'); END")
        with self.no_market_writes(), self.assertRaises(sqlite3.DatabaseError):
            self.engine.pairs.enable("gold", True)
        self.assertEqual(self.f.store.pair("gold"), pair)
        self.assertEqual(self.f.store.get("pair_runtime:gold"), original)
        self.assertEqual(self.event_rows(), events)


class PairPausedAdoptionSnapshotTests(unittest.TestCase):
    create = integration.PairIntegrationTests.create
    tearDown = integration.PairSnapshotLifecycleTests.tearDown

    def setUp(self):
        integration.PairSnapshotLifecycleTests.setUp(self)
        # Fixture APIs have no signing or network path; identities are public,
        # deliberately fake addresses needed by the production identity guard.
        for index, broker in enumerate(self.live.values(), 1):
            broker.api.credentials = {"user": "0x" + str(index) * 40, "signer": "0x" + str(index + 2) * 40}

    def seed(self):
        self.create()
        identities = {}
        for key, aid, side, quantity in (("long", "test", "LONG", "0.4"), ("short", "second", "SHORT", "0.6")):
            broker = self.live[aid]
            rows = broker.api.responses[ACCOUNT]["positions"]
            for row in rows:
                if row["symbol"] == SYMBOL and row["positionSide"] == side:
                    row.update(positionAmt=quantity if side == "LONG" else "-" + quantity, entryPrice="100")
            broker.api.responses[RISK] = [risk_row(row) for row in rows]
            credentials = broker.api.credentials
            member = self.f.store.account(aid)
            identities[key] = {"account_id": aid, "env_prefix": member["env_prefix"], "mode": "live", "side": side,
                               "user": credentials["user"], "signer": credentials["signer"]}
        state = {"attention": "人工调整后待核对", "owned": {"LONG": "0", "SHORT": "0"},
                 "pending": None, "progress": empty_progress(), "identities": identities}
        self.f.store.put("pair_runtime:gold", state)
        return state

    def test_expired_snapshot_after_master_verification_cannot_adopt(self):
        original = self.seed()
        read = self.engine.pairs._read_members
        snapshots = []

        def capture(*args, **kwargs):
            result = read(*args, **kwargs)
            snapshots.extend(result[0].values())
            return result

        self.verify.side_effect = lambda pair: setattr(snapshots[0], "timestamp", time.time() - 60)
        with patch.object(self.engine.pairs, "_read_members", side_effect=capture), self.assertRaisesRegex(TradingError, "过期"):
            self.engine.pairs.enable("gold", True)
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertEqual(self.f.store.get("pair_runtime:gold"), original)

    def test_generation_revoked_during_master_verification_cannot_adopt(self):
        original = self.seed()
        self.verify.side_effect = lambda pair: self.live["second"]._cycle_account_event("ACCOUNT_UPDATE")
        with self.assertRaisesRegex(TradingError, "失效"):
            self.engine.pairs.enable("gold", True)
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertEqual(self.f.store.get("pair_runtime:gold"), original)

    def test_preexisting_real_account_identity_change_cannot_adopt(self):
        original = self.seed()
        self.live["test"].api.credentials["user"] = "0x" + "9" * 40
        with self.assertRaises(TradingError):
            self.engine.pairs.enable("gold", True)
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertEqual(self.f.store.get("pair_runtime:gold"), original)

    def test_real_account_identity_change_during_verification_cannot_adopt(self):
        original = self.seed()
        self.verify.side_effect = lambda pair: self.live["test"].api.credentials.update(user="0x" + "9" * 40)
        with self.assertRaises(TradingError):
            self.engine.pairs.enable("gold", True)
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertEqual(self.f.store.get("pair_runtime:gold"), original)

    def test_current_live_get_fixtures_allow_unequal_baseline_adoption(self):
        self.seed()
        self.engine.pairs.enable("gold", True)
        self.assertTrue(self.f.store.pair("gold")["enabled"])
        state = self.f.store.get("pair_runtime:gold")
        self.assertEqual(state["owned"], {"LONG": "0.4", "SHORT": "0.6"})
        self.assertEqual(state["progress"]["baseline"], state["owned"])
        self.assertNotIn("attention", state)

    def test_snapshot_expiring_while_waiting_for_database_writer_is_rejected(self):
        original = self.seed()
        other = Store(self.f.store.path)
        acquired, attempting = threading.Event(), threading.Event()
        snapshots = []
        original_read = self.engine.pairs._read_members
        original_activate = self.f.store.activate_pair
        original_connect = self.f.store.connect

        def read(*args, **kwargs):
            result = original_read(*args, **kwargs)
            snapshots.extend(result[0].values())
            return result

        def hold_writer():
            with other.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                acquired.set()
                self.assertTrue(attempting.wait(2))
                snapshots[0].timestamp = time.time() - 60

        class Connection:
            def __init__(inner, db):
                inner.db = db

            def execute(inner, sql, *args):
                if sql == "BEGIN IMMEDIATE":
                    attempting.set()
                return inner.db.execute(sql, *args)

        @contextmanager
        def connect():
            with original_connect() as db:
                yield Connection(db)

        def activate(*args, **kwargs):
            with ThreadPoolExecutor(max_workers=1) as pool:
                holder = pool.submit(hold_writer)
                self.assertTrue(acquired.wait(2))
                try:
                    with patch.object(self.f.store, "connect", side_effect=connect):
                        return original_activate(*args, **kwargs)
                finally:
                    attempting.set()
                    holder.result(timeout=2)

        with patch.object(self.engine.pairs, "_read_members", side_effect=read), \
                patch.object(self.f.store, "activate_pair", side_effect=activate), \
                self.assertRaisesRegex(TradingError, "过期"):
            self.engine.pairs.enable("gold", True)
        self.assertFalse(self.f.store.pair("gold")["enabled"])
        self.assertEqual(self.f.store.get("pair_runtime:gold"), original)

    def test_account_event_waits_until_adoption_transaction_commits(self):
        self.seed()
        entered, finished = threading.Event(), threading.Event()
        committed = []
        broker = self.live["test"]
        original_lock = broker._snapshot_lock
        original_activate = self.f.store.activate_pair

        class ObservedLock:
            def __enter__(inner):
                if threading.current_thread().name == "pair-adoption-event":
                    entered.set()
                return original_lock.__enter__()

            def __exit__(inner, *args):
                return original_lock.__exit__(*args)

        def event():
            broker._cycle_account_event("ACCOUNT_UPDATE")
            committed.append((self.f.store.pair("gold")["enabled"], self.f.store.get("pair_runtime:gold")["owned"]))
            finished.set()

        worker = threading.Thread(target=event, name="pair-adoption-event", daemon=True)

        def activate(*args, **kwargs):
            worker.start()
            self.assertTrue(entered.wait(2))
            self.assertFalse(finished.wait(.02))
            return original_activate(*args, **kwargs)

        with patch.object(broker, "_snapshot_lock", ObservedLock()), \
                patch.object(self.f.store, "activate_pair", side_effect=activate):
            try:
                self.engine.pairs.enable("gold", True)
            finally:
                worker.join(timeout=2)
        self.assertTrue(finished.is_set())
        self.assertEqual(committed, [(True, {"LONG": "0.4", "SHORT": "0.6"})])
