"""Anonymous demo exposure must never claim a protected or unknown ledger."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from trading import server
from trading.engine import Engine
from trading.models import TradingError
from trading.paper import DemoMarket
from trading.store import Store
from .helpers import account


class DemoIsolationTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        root = patch.object(server, "ROOT", self.root)
        root.start()
        self.addCleanup(root.stop)
        environment = patch.dict(os.environ, {"ASTER_DASHBOARD_PASSWORD": "isolation-test-password"}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        # The normal app path must use simulated quotes as well: these checks
        # never start workers, contact the exchange or consume server credentials.
        engines = patch.object(server, "Engine", side_effect=lambda store, demo=False:
                               Engine(store, demo=demo, market=DemoMarket()))
        self.engine_factory = engines.start()
        self.addCleanup(engines.stop)

    def app(self, *, demo=False, runtime=None, engine=None):
        if runtime is None:
            return server.create_app(engine, demo=demo, start_engine=False)
        with patch.dict(os.environ, {"ASTER_TRADING_RUNTIME": str(runtime)}):
            return server.create_app(engine, demo=demo, start_engine=False)

    @staticmethod
    def pending(account_id):
        return {"id": "unconfirmed-batch", "account_id": account_id, "kind": "pair", "symbol": "XAUUSD1",
                "status": "pending", "orders": [], "receipts": {"original": {"status": "NEW"}}}

    def test_factory_default_demo_is_separate_and_allows_anonymous_paper_state(self):
        normal = self.app()
        demo = self.app(demo=True)
        self.assertEqual(normal.state.engine.store.path, (self.root / "runtime/trading/trading.sqlite3").resolve())
        self.assertEqual(demo.state.engine.store.path, (self.root / "runtime/demo/trading.sqlite3").resolve())
        with TestClient(normal) as client:
            self.assertEqual(client.get("/api/state").status_code, 401)
        with TestClient(demo) as client:
            response = client.get("/api/state")
            self.assertEqual(response.status_code, 200)
            self.assertEqual([a["mode"] for a in response.json()["accounts"]], ["paper"])

    def test_cli_and_factory_share_demo_path_without_changing_environment(self):
        captured = []
        with patch.object(server.socket, "socket"), patch("uvicorn.Server"), \
                patch("uvicorn.Config", side_effect=lambda app, **kwargs: captured.append(app)), \
                patch.object(sys, "argv", ["trading.server", "--demo"]):
            server.main()
        self.assertEqual(captured[0].state.engine.store.path, (self.root / "runtime/demo/trading.sqlite3").resolve())
        self.assertNotIn("ASTER_TRADING_RUNTIME", os.environ)

    def test_unknown_existing_paper_and_live_accounts_cannot_become_anonymous(self):
        for mode in ("paper", "live"):
            with self.subTest(mode=mode):
                runtime = self.root / mode
                store = Store(runtime / "trading.sqlite3")
                store.save_account(account(mode=mode))
                pending = self.pending("test")
                store.save_intent(pending)
                before = store.account("test")
                self.engine_factory.reset_mock()
                with self.assertRaisesRegex(TradingError, "用途未确认"):
                    self.app(demo=True, runtime=runtime)
                self.engine_factory.assert_not_called()
                self.assertEqual(store.account("test"), before)
                self.assertEqual(store.intent("test"), pending)
                self.assertIsNone(store.get("runtime_mode"))

    def test_every_existing_state_table_blocks_unmarked_demo_claim(self):
        writes = {
            "kv": lambda store: store.put("private-setting", {"value": "retain"}),
            "intents": lambda store: store.save_intent(self.pending("test")),
            "events": lambda store: store.event("test", "control", "retain event"),
            "outbox": lambda store: self.execute(store,
                "INSERT INTO outbox(id,message,due_at) VALUES ('pending','retain notification',0)"),
            "unknown": lambda store: self.execute(store, "CREATE TABLE legacy_private_state(value TEXT)"),
        }
        for name, write in writes.items():
            with self.subTest(table=name):
                runtime = self.root / name
                store = Store(runtime / "trading.sqlite3")
                write(store)
                with self.assertRaisesRegex(TradingError, "用途未确认"):
                    self.app(demo=True, runtime=runtime)
                self.assertIsNone(store.get("runtime_mode"))

    @staticmethod
    def execute(store, sql):
        with store.connect() as db:
            db.execute(sql)

    def test_named_demo_directory_does_not_grant_unknown_ledger_ownership(self):
        runtime = self.root / "runtime/demo"
        store = Store(runtime / "trading.sqlite3")
        store.save_account(account())
        with self.assertRaisesRegex(TradingError, "用途未确认"):
            self.app(demo=True)
        self.assertIsNotNone(store.account("test"))

    def test_legacy_protected_ledger_is_adopted_only_by_authenticated_app(self):
        runtime = self.root / "legacy"
        store = Store(runtime / "trading.sqlite3")
        store.save_account(account())
        pending = self.pending("test")
        store.save_intent(pending)
        before = store.account("test")
        app = self.app(runtime=runtime)
        self.assertEqual(store.get("runtime_mode"), "authenticated")
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/state").status_code, 401)
        self.assertEqual(store.account("test"), before)
        self.assertEqual(store.intent("test"), pending)
        self.app(runtime=runtime)
        self.assertEqual(store.intent("test"), pending)
        with self.assertRaisesRegex(TradingError, "用途与启动模式不符"):
            self.app(demo=True, runtime=runtime)

    def legacy_database(self, path, *, marker=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        old_account = account()
        old_account["policy"]["min_open_leverage"] = 125
        raw_account = json.dumps(old_account, ensure_ascii=False, indent=2)
        raw_intent = json.dumps(self.pending("test"), indent=3)
        with closing(sqlite3.connect(path)) as db, db:
            # Build pre-migration state without invoking Store's normalizers.
            db.executescript("""
                CREATE TABLE accounts(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE intents(id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                                     status TEXT NOT NULL, data TEXT NOT NULL);
                CREATE TABLE outbox(id TEXT PRIMARY KEY, message TEXT NOT NULL,
                                    attempts INTEGER NOT NULL DEFAULT 0, due_at REAL NOT NULL, delivered_at REAL);
            """)
            db.execute("INSERT INTO accounts VALUES ('test',?)", (raw_account,))
            db.execute("INSERT INTO intents VALUES ('unconfirmed-batch','test','pending',?)", (raw_intent,))
            db.execute("INSERT INTO outbox(id,message,due_at) VALUES ('old-notification','retain original',0)")
            if marker is not None:
                db.execute("CREATE TABLE kv(key TEXT PRIMARY KEY, data TEXT NOT NULL)")
                db.execute("INSERT INTO kv VALUES ('runtime_mode',?)", (json.dumps(marker),))
        return raw_account, raw_intent

    def test_rejected_demo_does_not_migrate_raw_legacy_configuration_or_notifications(self):
        for marker in (None, "authenticated"):
            with self.subTest(marker=marker):
                runtime = self.root / str(marker)
                path = runtime / "trading.sqlite3"
                raw_account, raw_intent = self.legacy_database(path, marker=marker)
                original = path.read_bytes()
                self.engine_factory.reset_mock()
                with self.assertRaises(TradingError):
                    self.app(demo=True, runtime=runtime)
                self.engine_factory.assert_not_called()
                self.assertEqual(path.read_bytes(), original)
                with closing(sqlite3.connect(path)) as db, db:
                    self.assertEqual(db.execute("SELECT data FROM accounts").fetchone()[0], raw_account)
                    self.assertEqual(db.execute("SELECT data FROM intents").fetchone()[0], raw_intent)
                    columns = {row[1] for row in db.execute("PRAGMA table_info(outbox)")}
                    self.assertNotIn("expires_at", columns)
                    self.assertNotIn("capacity_key", columns)

    def test_rejected_normal_mode_does_not_migrate_raw_demo_ledger(self):
        path = self.root / "old-demo/trading.sqlite3"
        self.legacy_database(path, marker="demo")
        original = path.read_bytes()
        with self.assertRaisesRegex(TradingError, "用途与启动模式不符"):
            self.app(runtime=path.parent)
        self.assertEqual(path.read_bytes(), original)

    def test_legacy_normal_adoption_still_runs_required_migrations(self):
        path = self.root / "old-normal/trading.sqlite3"
        _, raw_intent = self.legacy_database(path)
        store = Store(path, demo=False)
        self.assertEqual(store.get("runtime_mode"), "authenticated")
        self.assertFalse(store.account("test")["enabled"])
        self.assertTrue(store.account("test")["leverage_setting_required"])
        self.assertEqual(store.account("test")["policy"]["min_open_leverage"], 20)
        with store.connect() as db:
            self.assertEqual(db.execute("SELECT data FROM intents").fetchone()[0], raw_intent)
            columns = {row[1] for row in db.execute("PRAGMA table_info(outbox)")}
            self.assertTrue({"expires_at", "capacity_key"}.issubset(columns))
            self.assertEqual(db.execute("SELECT message FROM outbox").fetchone()[0], "retain original")

    def test_initialization_failure_rolls_back_mode_schema_and_state_migrations(self):
        path = self.root / "failed-migration/trading.sqlite3"
        self.legacy_database(path)
        original = path.read_bytes()
        with patch.object(Store, "account_defaults", side_effect=RuntimeError("migration interrupted")):
            with self.assertRaisesRegex(RuntimeError, "migration interrupted"):
                Store(path, demo=False)
        self.assertEqual(path.read_bytes(), original)
        with closing(sqlite3.connect(path)) as db, db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertNotIn("kv", tables)
            self.assertNotIn("events", tables)
        self.assertEqual(Store(path, demo=False).get("runtime_mode"), "authenticated")

    def test_demo_claim_rollback_keeps_incompatible_empty_schema_unchanged(self):
        path = self.root / "incompatible.sqlite3"
        with closing(sqlite3.connect(path)) as db, db:
            db.execute("CREATE TABLE outbox(id TEXT PRIMARY KEY)")
        original = path.read_bytes()
        with self.assertRaises(sqlite3.OperationalError):
            Store(path, demo=True)
        self.assertEqual(path.read_bytes(), original)
        with closing(sqlite3.connect(path)) as db, db:
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertEqual(tables, {"outbox"})

    def test_bound_empty_authenticated_ledger_stays_protected(self):
        runtime = self.root / "empty-protected"
        self.app(runtime=runtime)
        with self.assertRaisesRegex(TradingError, "用途与启动模式不符"):
            self.app(demo=True, runtime=runtime)
        self.assertEqual(Store(runtime / "trading.sqlite3").accounts(), [])

    def test_demo_restart_preserves_data_and_pending_intent_and_rejects_normal_mode(self):
        runtime = self.root / "custom-demo"
        first = self.app(demo=True, runtime=runtime)
        store = first.state.engine.store
        saved = store.account("demo")
        saved["name"] = "保留的演示账户"
        store.save_account(saved)
        pending = self.pending("demo")
        store.save_intent(pending)
        paper = store.get("paper:demo")
        paper["wallet"] = "12345"
        store.put("paper:demo", paper)
        second = self.app(demo=True, runtime=runtime / ".")
        self.assertEqual(second.state.engine.store.account("demo"), saved)
        self.assertEqual(second.state.engine.store.intent("demo"), pending)
        self.assertEqual(second.state.engine.store.get("paper:demo"), paper)
        with self.assertRaisesRegex(TradingError, "用途与启动模式不符"):
            self.app(runtime=runtime)
        self.assertEqual(store.intent("demo"), pending)

    def test_injected_demo_engine_cannot_bypass_protected_or_unknown_store(self):
        for marked in (False, True):
            with self.subTest(marked=marked):
                store = Store(self.root / str(marked) / "trading.sqlite3")
                store.save_account(account())
                pending = self.pending("test")
                store.save_intent(pending)
                if marked:
                    store.bind_runtime_mode(demo=False)
                engine = Engine(store, demo=True, market=DemoMarket())
                with patch.object(engine, "start") as start:
                    # The injected Engine's actual mode controls authentication,
                    # even when the caller leaves the factory demo flag false.
                    with self.assertRaises(TradingError):
                        self.app(engine=engine)
                    start.assert_not_called()
                self.assertEqual(store.intent("test"), pending)

    def test_injected_demo_engine_requires_empty_store_binding_before_seeding(self):
        unbound = Store(self.root / "unbound.sqlite3")
        engine = Engine(unbound, demo=True, market=DemoMarket())
        with self.assertRaisesRegex(TradingError, "用途未确认"):
            self.app(engine=engine)
        bound = Store(self.root / "bound.sqlite3")
        bound.bind_runtime_mode(demo=True)
        engine = Engine(bound, demo=True, market=DemoMarket())
        app = self.app(engine=engine)
        with TestClient(app) as client:
            self.assertEqual(client.get("/api/state").status_code, 200)
        self.assertEqual(bound.get("runtime_mode"), "demo")

    def test_invalid_marker_fails_closed_for_both_modes(self):
        for value in (None, {}, "other"):
            for demo in (False, True):
                with self.subTest(value=value, demo=demo):
                    store = Store(self.root / "invalid.sqlite3")
                    store.put("runtime_mode", value)
                    with self.assertRaisesRegex(TradingError, "用途标记无效"):
                        store.bind_runtime_mode(demo=demo)
                    self.assertEqual(store.get("runtime_mode"), value)

    def test_competing_store_instances_cannot_bind_different_modes(self):
        path = self.root / "race.sqlite3"
        stores = [Store(path), Store(path)]
        barrier = threading.Barrier(2)

        def claim(index):
            barrier.wait(timeout=5)
            try:
                stores[index].bind_runtime_mode(demo=bool(index))
                return index
            except TradingError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(claim, (0, 1)))
        winners = [value for value in results if value is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(stores[0].get("runtime_mode"), "demo" if winners[0] else "authenticated")

    def test_competing_initializations_cannot_claim_a_new_database_in_different_modes(self):
        path = self.root / "initialization-race.sqlite3"
        barrier = threading.Barrier(2)

        def initialize(index):
            barrier.wait(timeout=5)
            try:
                Store(path, demo=bool(index))
                return index
            except TradingError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(initialize, (0, 1)))
        winners = [value for value in results if value is not None]
        self.assertEqual(len(winners), 1)
        self.assertEqual(Store(path).get("runtime_mode"), "demo" if winners[0] else "authenticated")
