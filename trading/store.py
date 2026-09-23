"""Durable execution intents and notification outbox, isolated per account."""
from contextlib import contextmanager
from datetime import datetime, timezone
from fractions import Fraction
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from .models import MIN_OPEN_LEVERAGE, SYMBOLS, TIERS, TradingError, dec, positive, wire
from .migration import DEFAULT_MIGRATION
from .cycle import DEFAULT_CYCLE
from .ledger_cache import LedgerCache
from .account_deletion import deletion_block
from .request_timing import database_clock, database_duration
from .cycle_volume import (FILL_FIELDS, account_identifier, event_message, identifier, normalize_fill,
                           order_bindings, receipt_quantity, sort_key, utc_day, validate_fill_binding)


CAPACITY_ALERT_MAX_AGE = 8
CAPACITY_ALERT_KEYS = frozenset(f"capacity_alert:{symbol}:{tier}" for symbol in SYMBOLS for tier in TIERS)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _cycle_check_metadata(raw):
    """Unreadable historical metadata is a boundary, never a reason to lose an event."""
    try:
        value = json.loads(raw)
        required = {"symbol", "phase", "count", "first_at", "last_at"}
        if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"diagnostic"}:
            return None
        if value["symbol"] not in SYMBOLS or value["phase"] not in ("open", "close"):
            return None
        if type(value["count"]) is not int or value["count"] < 1:
            return None
        if any(type(value[key]) not in (int, float) or not math.isfinite(value[key]) or not 0 <= value[key] <= 253402300799
               for key in ("first_at", "last_at")) or value["first_at"] > value["last_at"]:
            return None
        if "diagnostic" in value:
            diagnostic = value["diagnostic"]
            if not isinstance(diagnostic, dict) or diagnostic.get("symbol", value["symbol"]) != value["symbol"] \
                    or diagnostic.get("phase", value["phase"]) != value["phase"]:
                return None
        # json.loads accepts non-finite literals by default; these must not leak
        # through the API or make an invalid record eligible for aggregation.
        json.dumps(value, allow_nan=False)
        return value
    except (TypeError, ValueError, OverflowError, RecursionError):
        return None


class Store:
    def __init__(self, path, *, demo=None):
        # All aliases of one database must share its process lock and WAL files.
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._connection_local = threading.local()
        self._rolling_cache = LedgerCache()
        with self.connect() as db:
            # Decide HTTP exposure before touching historical schema or state.
            # Keep the claim and migrations in one rollback-capable transaction;
            # executescript would implicitly commit the claim before migrating.
            db.execute("BEGIN IMMEDIATE")
            if demo is not None:
                self._bind_runtime_mode(db, demo=demo)
            had_volume_index = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cycle_volume_sync'").fetchone() is not None
            had_symbol_volume = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cycle_symbol_volume_days'").fetchone() is not None
            schema = """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS deleted_accounts (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL, deleted_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS intents (
                    id TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    status TEXT NOT NULL, data TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_active_intent
                    ON intents(account_id) WHERE status NOT IN ('complete','aborted');
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                    kind TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL,
                    cycle_check TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_events_account ON events(account_id,id);
                CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC,id DESC);
                CREATE INDEX IF NOT EXISTS idx_events_account_created ON events(account_id,created_at DESC,id DESC);
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, message TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    due_at REAL NOT NULL, delivered_at REAL, expires_at REAL, capacity_key TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(due_at) WHERE delivered_at IS NULL;
                CREATE TABLE IF NOT EXISTS cycle_fills (
                    account_id TEXT NOT NULL, symbol TEXT NOT NULL, trade_id TEXT NOT NULL,
                    order_id TEXT NOT NULL, client_id TEXT NOT NULL, intent_id TEXT NOT NULL,
                    phase TEXT NOT NULL, position_side TEXT NOT NULL, side TEXT NOT NULL,
                    quantity TEXT NOT NULL, price TEXT NOT NULL, notional TEXT NOT NULL,
                    executed_at REAL NOT NULL, time_source TEXT NOT NULL, utc_date TEXT NOT NULL,
                    daily_volume TEXT NOT NULL, recorded_at REAL NOT NULL, event_id INTEGER,
                    PRIMARY KEY(account_id,symbol,trade_id)
                );
                CREATE INDEX IF NOT EXISTS idx_cycle_fills_day
                    ON cycle_fills(account_id,utc_date,executed_at,symbol,trade_id);
                CREATE INDEX IF NOT EXISTS idx_cycle_fills_order
                    ON cycle_fills(intent_id,client_id);
                CREATE INDEX IF NOT EXISTS idx_cycle_fills_recent
                    ON cycle_fills(account_id,executed_at DESC,symbol DESC,trade_id DESC);
                CREATE INDEX IF NOT EXISTS idx_cycle_fills_exchange_order
                    ON cycle_fills(account_id,symbol,order_id,client_id,intent_id);
                CREATE INDEX IF NOT EXISTS idx_cycle_fills_client
                    ON cycle_fills(account_id,symbol,client_id,order_id,intent_id);
                CREATE TABLE IF NOT EXISTS cycle_fill_versions (
                    account_id TEXT PRIMARY KEY, revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS cycle_symbol_volume_days (
                    account_id TEXT NOT NULL, utc_date TEXT NOT NULL, volume TEXT NOT NULL,
                    trade_count INTEGER NOT NULL, estimated_volume TEXT NOT NULL,
                    estimated_trade_count INTEGER NOT NULL, latest_trade_at REAL,
                    latest_symbol TEXT, latest_trade_id TEXT, updated_at REAL NOT NULL,
                    symbol TEXT NOT NULL, PRIMARY KEY(account_id,utc_date,symbol)
                );
                CREATE INDEX IF NOT EXISTS idx_cycle_fills_symbol_time
                    ON cycle_fills(account_id,symbol,executed_at,trade_id);
                CREATE INDEX IF NOT EXISTS idx_cycle_fills_symbol_day
                    ON cycle_fills(account_id,utc_date,symbol,executed_at,trade_id);
                CREATE TABLE IF NOT EXISTS cycle_volume_sync (
                    intent_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL, completed_at REAL, synced_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_cycle_volume_backlog
                    ON cycle_volume_sync(account_id,synced_at,status,completed_at,created_at);
            """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            # A revision changes in the same transaction as its fills, including
            # backfills and writes from a different process/Store. Rollbacks
            # cannot publish a new revision. Never infer validity from a TTL.
            for action, owners in (("INSERT", ("NEW",)), ("DELETE", ("OLD",)), ("UPDATE", ("OLD", "NEW"))):
                updates = " ".join(
                    f"INSERT INTO cycle_fill_versions SELECT {owner}.account_id,1 WHERE "
                    + ("NEW.account_id!=OLD.account_id " if action == "UPDATE" and owner == "NEW" else "1 ") +
                    "ON CONFLICT(account_id) DO UPDATE SET revision=revision+1;"
                    for owner in owners)
                db.execute(f"DROP TRIGGER IF EXISTS cycle_fills_revision_{action.lower()}")
                db.execute(f"CREATE TRIGGER IF NOT EXISTS cycle_fills_revision_{action.lower()} "
                           f"AFTER {action} ON cycle_fills BEGIN {updates} END")
            # Existing trade notifications keep NULL expiry and remain deliverable.
            columns = {row[1] for row in db.execute("PRAGMA table_info(outbox)")}
            for name, kind in (("expires_at", "REAL"), ("capacity_key", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE outbox ADD COLUMN {name} {kind}")
            event_columns = {row[1] for row in db.execute("PRAGMA table_info(events)")}
            if "cycle_check" not in event_columns:
                db.execute("ALTER TABLE events ADD COLUMN cycle_check TEXT")
            for row in db.execute("SELECT id,data FROM accounts").fetchall():
                account = json.loads(row["data"])
                normalized = self.account_defaults(account)
                if normalized != account:
                    db.execute("UPDATE accounts SET data=? WHERE id=?", (dumps(normalized), row["id"]))
            # One upgrade pass builds the lightweight index. Subsequent account
            # ticks never deserialize all historical intents to find today's work.
            if not had_volume_index:
                for row in db.execute("SELECT id,account_id,status,data FROM intents").fetchall():
                    self._index_cycle_volume(db, json.loads(row["data"]), row=row)
            if not had_symbol_volume:
                for group in db.execute("SELECT DISTINCT account_id,utc_date,symbol FROM cycle_fills").fetchall():
                    rows = [dict(row) for row in db.execute(
                        "SELECT * FROM cycle_fills WHERE account_id=? AND utc_date=? AND symbol=?",
                        tuple(group))]
                    self._update_cycle_day(db, group["account_id"], group["utc_date"], rows, symbol=group["symbol"])
            # The all-symbol total is derived from at most three symbol rows.
            # Its old duplicate aggregate is no longer read or maintained.
            db.execute("DROP TABLE IF EXISTS cycle_volume_days")
            placeholders = ",".join("?" for _ in CAPACITY_ALERT_KEYS)
            db.execute(f"""UPDATE outbox SET expires_at=0 WHERE delivered_at IS NULL
                AND capacity_key IS NOT NULL AND capacity_key NOT IN ({placeholders})""", tuple(CAPACITY_ALERT_KEYS))
        # WAL cannot be enabled inside the initialization transaction. A rejected
        # mode or failed migration never reaches these database-level changes.
        with self.connect() as db:
            # A competing initializer can hold a lock after our claim commits.
            # journal_mode may return BUSY immediately despite busy_timeout, so
            # bound this transition itself to the same ten-second wait budget.
            deadline = time.monotonic() + 10
            db.execute("PRAGMA busy_timeout=0")
            while True:
                try:
                    mode = db.execute("PRAGMA journal_mode=WAL").fetchone()
                    if not mode or mode[0] != "wal":
                        raise sqlite3.OperationalError("无法启用 SQLite WAL 日志模式")
                    break
                except sqlite3.OperationalError as exc:
                    code = getattr(exc, "sqlite_errorcode", None)
                    # SQLite primary BUSY/LOCKED are 5/6; extended codes keep
                    # their primary code in the low byte. Python 3.10 has no
                    # sqlite_errorcode, so accept only its exact lock messages.
                    locked = (code & 255) in (5, 6) if type(code) is int else code is None and str(exc) in (
                        "database is locked", "database table is locked")
                    remaining = deadline - time.monotonic()
                    if not locked or remaining <= 0:
                        raise
                    time.sleep(min(0.05, remaining))
            db.execute("PRAGMA busy_timeout=10000")
            db.execute("PRAGMA optimize")

    @contextmanager
    def connection_scope(self):
        """Reuse a thread's handle, never its transactions or the writer lock."""
        if getattr(self._connection_local, "scoped", False):
            yield
            return
        self._connection_local.scoped = True
        try:
            yield
        finally:
            db = getattr(self._connection_local, "db", None)
            self._connection_local.db = None
            self._connection_local.scoped = False
            if db is not None:
                db.close()

    @contextmanager
    def connect(self):
        waiting = database_clock()
        with self.lock:
            database_duration("lock_wait_ms", waiting)
            db = getattr(self._connection_local, "db", None)
            scoped = getattr(self._connection_local, "scoped", False)
            if db is None:
                started = database_clock()
                db = sqlite3.connect(self.path, timeout=10)
                db.row_factory = sqlite3.Row
                db.execute("PRAGMA synchronous=FULL")
                database_duration("connection_ms", started)
                if scoped:
                    self._connection_local.db = db
            try:
                with db:
                    yield db
            finally:
                if not scoped:
                    db.close()

    @contextmanager
    def capacity_alert_batch(self):
        """Commit a symbol's independent notification gates together."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            yield _StoreSnapshot(self, db)

    @contextmanager
    def read_snapshot(self):
        """Dashboard reads use a consistent WAL snapshot, outside the writer lock.

        SQLite enforces read-only access even if a caller accidentally invokes a
        mutating Store method. This connection never migrates or creates a DB.
        """
        db = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=.25)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            reader = _StoreSnapshot(self, db)
            yield reader
        finally:
            db.close()

    def bind_runtime_mode(self, *, demo):
        """Bind HTTP exposure before an Engine can seed or publish account data."""
        with self.connect() as db:
            # Different Store objects/processes must not claim an empty ledger
            # with different exposure modes between the check and the write.
            db.execute("BEGIN IMMEDIATE")
            self._bind_runtime_mode(db, demo=demo)

    @staticmethod
    def _bind_runtime_mode(db, *, demo):
        if type(demo) is not bool:
            raise TradingError("账本运行用途无效")
        mode = "demo" if demo else "authenticated"
        tables = {row["name"] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                  if not row["name"].startswith("sqlite_")}
        row = db.execute("SELECT data FROM kv WHERE key='runtime_mode'").fetchone() if "kv" in tables else None
        if row is not None:
            try:
                saved = json.loads(row["data"])
            except (TypeError, ValueError):
                raise TradingError("账本用途标记无效，拒绝启动") from None
            if saved not in ("demo", "authenticated"):
                raise TradingError("账本用途标记无效，拒绝启动")
            if saved != mode:
                raise TradingError("账本用途与启动模式不符；演示与正式服务必须使用独立的数据目录")
            return
        if demo:
            expected = {"accounts", "deleted_accounts", "kv", "intents", "events", "outbox", "cycle_fills",
                        "cycle_volume_days", "cycle_symbol_volume_days", "cycle_volume_sync", "cycle_fill_versions"}
            # New databases may have no tables yet. Historical paper data and
            # unknown tables must never be claimed by the anonymous interface.
            if tables - expected or any(db.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
                                        for table in sorted(tables)):
                raise TradingError("演示模式不能使用用途未确认的已有账本；请指定新的空数据目录，原数据保留")
        db.execute("CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
        db.execute("INSERT INTO kv(key,data) VALUES ('runtime_mode',?)", (dumps(mode),))

    @staticmethod
    def account_defaults(account):
        migration = account.get("migration")
        normalized = {**account, "migration": {**DEFAULT_MIGRATION, **migration} if isinstance(migration, dict)
                      else {**DEFAULT_MIGRATION} if "migration" not in account else migration}
        cycle = account.get("cycle")
        normalized["cycle"] = ({**DEFAULT_CYCLE, **cycle} if isinstance(cycle, dict)
                               else {**DEFAULT_CYCLE} if "cycle" not in account else cycle)
        policy = account.get("policy")
        if isinstance(policy, dict):
            policy = {"ordinary_symbol": "all", **policy}
            normalized["policy"] = policy
            previous = policy.get("min_open_leverage", MIN_OPEN_LEVERAGE)
            if type(previous) is int and 1 <= previous <= 125:
                minimum = next((tier for tier in TIERS if tier >= previous), TIERS[-1])
                normalized["policy"] = {**policy, "min_open_leverage": minimum}
                if previous > TIERS[-1]:
                    normalized.update(enabled=False, pause_reason="原最低开仓杠杆超出支持范围，已暂停；请在 5x、10x、20x 中确认设置后启动")
                    normalized["leverage_setting_required"] = True
        return normalized

    def accounts(self):
        with self.connect() as db:
            return [self.account_defaults(json.loads(r[0])) for r in db.execute("SELECT data FROM accounts ORDER BY id")]

    def account(self, account_id):
        with self.connect() as db:
            row = db.execute("SELECT data FROM accounts WHERE id=?", (account_id,)).fetchone()
            return self.account_defaults(json.loads(row[0])) if row else None

    def save_account(self, account):
        account = self.account_defaults(account)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT 1 FROM deleted_accounts WHERE id=?", (account["id"],)).fetchone():
                raise TradingError("该账户标识已有删除记录，请使用新的账户标识")
            db.execute("INSERT INTO accounts VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (account["id"], dumps(account)))

    def account_id_used(self, account_id):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM accounts WHERE id=? UNION ALL SELECT 1 FROM deleted_accounts WHERE id=?",
                              (account_id, account_id)).fetchone() is not None

    def delete_account(self, account_id):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            reader = _StoreSnapshot(self, db)
            account = reader.account(account_id)
            if account is None:
                if db.execute("SELECT 1 FROM deleted_accounts WHERE id=?", (account_id,)).fetchone():
                    return  # Retrying an acknowledged or timed-out deletion is safe.
                raise TradingError("账户不存在")
            reason = deletion_block(account, reader.intent(account_id), reader.get("post_fill_check:" + account_id),
                                    reader.get("cycle:" + account_id))
            if reason:
                raise TradingError(reason)
            now = time.time()
            db.execute("INSERT INTO deleted_accounts VALUES (?,?,?)", (account_id, dumps(account), now))
            db.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)",
                       (account_id, "config", "账户已从工作台删除，历史记录保留", now))

    def pause_account(self, account, reason):
        account.update(enabled=False, pause_reason=reason)
        self.save_account(account)

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT data FROM kv WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def put(self, key, value):
        with self.connect() as db:
            db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data", (key, dumps(value)))

    def intent(self, account_id):
        with self.connect() as db:
            row = db.execute("SELECT data FROM intents WHERE account_id=? AND status NOT IN ('complete','aborted')", (account_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_listing_state(self, state, alerts=()):
        """Persist discovery progress and its notification outbox atomically."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                       ("usd1_listings", dumps(state)))
            for notification_id, message in alerts:
                db.execute("INSERT OR IGNORE INTO outbox(id,message,due_at) VALUES (?,?,?)",
                           (notification_id, message, time.time()))

    def save_intent(self, intent):
        with self.connect() as db:
            db.execute("INSERT INTO intents VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data",
                       (intent["id"], intent["account_id"], intent["status"], dumps(intent)))
            self._index_cycle_volume(db, intent)

    def save_cycle_volume_state(self, intent, original):
        """Merge terminal backfill metadata without replacing execution state."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = self._cycle_ledger_intent(db, intent)
            status = db.execute("SELECT status FROM intents WHERE id=?", (intent["id"],)).fetchone()[0]
            if status not in ("complete", "aborted"):
                # Active reconciliation still owns this intent exclusively.
                db.execute("UPDATE intents SET status=?,data=? WHERE id=?",
                           (intent["status"], dumps(intent), intent["id"]))
                self._index_cycle_volume(db, intent)
                return False
            saved["status"] = status
            indexed = db.execute("SELECT synced_at FROM cycle_volume_sync WHERE intent_id=?", (intent["id"],)).fetchone()
            if indexed is not None and indexed[0] is not None:
                saved.pop("volume_error", None)
                saved["volume_synced"] = True
                db.execute("UPDATE intents SET data=? WHERE id=?", (dumps(saved), intent["id"]))
                return True
            bindings = order_bindings(saved)
            signatures = {}
            for cid, (order, receipt, _) in bindings.items():
                qty = receipt_quantity(order, receipt, require_terminal=True)
                incoming = intent.get("receipts", {}).get(cid)
                if incoming is not None and receipt_quantity(order, incoming) == qty:
                    old_id, new_id = receipt.get("orderId"), incoming.get("orderId")
                    if old_id is not None and new_id is not None and str(old_id) != str(new_id):
                        raise TradingError("循环补账订单编号与已完成回执冲突")
                    # Terminal economics and status stay fixed. A query or a
                    # legacy paper adapter may only add previously absent data.
                    receipt.update({key: value for key, value in incoming.items() if receipt.get(key) is None})
                signatures[cid] = [str(receipt.get("orderId", "")), wire(qty), receipt["status"]]
            confirmed = saved.setdefault("volume_receipts", {})
            prior_confirmed = dict(confirmed)
            for cid, signature in intent.get("volume_receipts", {}).items():
                if signature != signatures.get(cid):
                    continue
                recorded = sum((Fraction(dec(row[0])) for row in db.execute(
                    "SELECT quantity FROM cycle_fills WHERE intent_id=? AND client_id=?", (intent["id"], cid))), Fraction(0))
                if recorded == Fraction(dec(signature[1])):
                    confirmed[cid] = signature
            queries = saved.setdefault("volume_queries", {})
            before_queries = original.get("volume_queries") or {}
            for cid, incoming in intent.get("volume_queries", {}).items():
                if cid not in bindings:
                    continue
                if confirmed.get(cid) == signatures[cid]:
                    queries[cid] = {}
                    continue
                current = queries.get(cid, {})
                if current == before_queries.get(cid, {}):
                    queries[cid] = incoming
                elif incoming and current.get("identity") == incoming.get("identity"):
                    # Another worker advanced this checkpoint. Keep its cursor,
                    # but retain every matching fill observed by either reader.
                    fills = current.setdefault("fills", {})
                    for trade_id, fill in incoming.get("fills", {}).items():
                        if trade_id in fills and fills[trade_id] != fill:
                            raise TradingError("并发循环补账出现冲突成交明细")
                        fills[trade_id] = fill
            if intent.get("volume_error"):
                if prior_confirmed == (original.get("volume_receipts") or {}):
                    saved["volume_error"] = intent["volume_error"]
            else:
                saved.pop("volume_error", None)
            db.execute("UPDATE intents SET data=? WHERE id=?", (dumps(saved), intent["id"]))
            return False

    def create_cycle_intent(self, intent, message):
        """Commit a new pending cycle, its volume index and event before send."""
        if intent.get("kind") != "cycle" or intent.get("status") != "pending":
            raise TradingError("只能创建新的待提交循环批次")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            # This is creation, never an upsert of a previously submitted ID.
            db.execute("INSERT INTO intents VALUES (?,?,?,?)",
                       (intent["id"], intent["account_id"], intent["status"], dumps(intent)))
            self._index_cycle_volume(db, intent)
            db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)",
                       (intent["account_id"], "cycle", message, time.time()))

    def record_cycle_execution_quality(self, intent):
        """Update display metadata only; leave status and volume indexes intact."""
        quality = intent["execution_quality"]
        if not isinstance(quality, dict) or quality.get("intent_id") != intent.get("id"):
            raise TradingError("循环执行观测与批次不一致")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT account_id,data FROM intents WHERE id=?", (intent["id"],)).fetchone()
            if row is None or row["account_id"] != intent["account_id"]:
                raise TradingError("循环执行观测不属于此账户")
            saved = json.loads(row["data"])
            if saved.get("kind") != "cycle" or any(saved.get(key) != intent.get(key)
                                                     for key in ("account_id", "symbol", "phase", "quantity")):
                raise TradingError("循环执行观测与持久批次不一致")
            if any(quality.get(key) != saved.get(key) for key in ("symbol", "phase", "quantity", "created_at")):
                raise TradingError("循环执行观测元数据不一致")
            serialized = json.dumps(quality, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            saved["execution_quality"] = quality
            db.execute("UPDATE intents SET data=? WHERE id=?", (dumps(saved), intent["id"]))
            key = "cycle_execution:" + intent["account_id"]
            previous = db.execute("SELECT data FROM kv WHERE key=?", (key,)).fetchone()
            latest = json.loads(previous["data"]) if previous else None
            # Delayed historical reconciliation must not displace a newer batch.
            if isinstance(latest, dict) and latest.get("intent_id") != intent["id"] \
                    and latest.get("created_at", 0) >= quality["created_at"]:
                return
            db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data", (key, serialized))

    @staticmethod
    def _index_cycle_volume(db, intent, row=None):
        if intent.get("kind") != "cycle":
            return
        def timestamp(value):
            return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
        identity = row if row is not None else intent
        db.execute("""INSERT INTO cycle_volume_sync(intent_id,account_id,status,created_at,completed_at,synced_at)
                    VALUES (?,?,?,?,?,NULL) ON CONFLICT(intent_id) DO UPDATE SET
                    status=excluded.status,created_at=excluded.created_at,completed_at=excluded.completed_at""",
                   (identity["id"], identity["account_id"], identity["status"],
                    timestamp(intent.get("created_at")), timestamp(intent.get("completed_at"))))

    @staticmethod
    def _cycle_ledger_intent(db, intent):
        if not isinstance(intent, dict):
            raise TradingError("循环交易量批次无效")
        intent_id = identifier(intent.get("id"), "循环批次标识")
        account_id = account_identifier(intent.get("account_id"))
        row = db.execute("SELECT account_id,status,data FROM intents WHERE id=?", (intent_id,)).fetchone()
        if row is None or row["account_id"] != account_id:
            raise TradingError("循环逐笔成交批次不存在或不属于此账户")
        saved = json.loads(row["data"])
        if (saved.get("account_id") != account_id or saved.get("id") != intent_id
                or saved.get("kind") != "cycle" or any(saved.get(key) != intent.get(key) for key in ("kind", "symbol", "run_id"))):
            raise TradingError("循环逐笔成交与持久批次身份不一致")
        if db.execute("SELECT 1 FROM accounts WHERE id=?", (account_id,)).fetchone() is None:
            raise TradingError("循环成交账户不存在")
        return saved

    def record_cycle_fills(self, intent, fills):
        """Atomically admit individually verified fills; return the new row count."""
        if not isinstance(fills, (list, tuple)) or len(fills) > 20000:
            raise TradingError("循环逐笔成交列表无效或过大")
        normalized = [normalize_fill(fill) for fill in fills]
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = self._cycle_ledger_intent(db, intent)
            account_id, intent_id = saved["account_id"], saved["id"]
            bindings = order_bindings(saved)
            limits, changed_days = {}, {}
            for fill in normalized:
                phase, maximum_quantity = validate_fill_binding(fill, bindings)
                limits[fill["client_id"]] = maximum_quantity
                row = {**fill, "account_id": account_id, "intent_id": intent_id, "phase": phase,
                       "daily_volume": "0", "recorded_at": time.time()}
                previous = db.execute("SELECT * FROM cycle_fills WHERE account_id=? AND symbol=? AND trade_id=?",
                                      (account_id, fill["symbol"], fill["trade_id"])).fetchone()
                if previous is not None:
                    if any(previous[key] != row[key] for key in FILL_FIELDS | {"intent_id", "phase", "utc_date"}):
                        raise TradingError("同一循环成交标识对应的价量、时间或委托发生冲突")
                    continue
                if db.execute("SELECT 1 FROM cycle_fills WHERE account_id=? AND symbol=? AND order_id=? AND (client_id!=? OR intent_id!=?) LIMIT 1",
                              (account_id, fill["symbol"], fill["order_id"], fill["client_id"], intent_id)).fetchone():
                    raise TradingError("同一交易所订单号不能归属不同循环委托")
                if db.execute("SELECT 1 FROM cycle_fills WHERE account_id=? AND symbol=? AND client_id=? AND (order_id!=? OR intent_id!=?) LIMIT 1",
                              (account_id, fill["symbol"], fill["client_id"], fill["order_id"], intent_id)).fetchone():
                    raise TradingError("同一循环委托不能归属不同交易所订单号或批次")
                event = db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)",
                                   (account_id, "cycle_fill", "循环逐笔成交入账", row["recorded_at"]))
                row["event_id"] = event.lastrowid
                fields = (*FILL_FIELDS, "account_id", "intent_id", "phase", "utc_date", "daily_volume", "recorded_at", "event_id")
                db.execute("INSERT INTO cycle_fills(" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ")",
                           tuple(row[key] for key in fields))
                changed_days.setdefault(fill["utc_date"], []).append(row)
            for client_id, maximum_quantity in limits.items():
                quantity = sum((Fraction(dec(row[0])) for row in db.execute(
                    "SELECT quantity FROM cycle_fills WHERE intent_id=? AND client_id=?", (intent_id, client_id))), Fraction(0))
                if quantity > maximum_quantity:
                    raise TradingError("循环逐笔成交累计数量超过已核实回执")
            for date, rows in changed_days.items():
                for symbol in {row["symbol"] for row in rows}:
                    self._update_cycle_day(db, account_id, date, [row for row in rows if row["symbol"] == symbol], symbol=symbol)
            return sum(len(rows) for rows in changed_days.values())

    @staticmethod
    def _update_cycle_day(db, account_id, date, new_rows, *, symbol):
        table = "cycle_symbol_volume_days"
        where = "account_id=? AND utc_date=? AND symbol=?"
        params = (account_id, date, symbol)
        old = db.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchone()
        # The preceding persisted prefix seeds a late fill's affected suffix.
        # Aggregate counts only change by the newly admitted rows.
        latest = (old["latest_trade_at"], old["latest_symbol"], old["latest_trade_id"]) if old else None
        append = old is not None and all(sort_key(row) > latest for row in new_rows)
        rows = sorted(new_rows, key=sort_key)
        volume = Fraction(dec(old["volume"])) if append else Fraction(0)
        if old is not None and not append:
            first = rows[0]["executed_at"], rows[0]["trade_id"]
            previous = db.execute(f"SELECT daily_volume FROM cycle_fills WHERE {where} "
                "AND (executed_at,trade_id)<(?,?) ORDER BY executed_at DESC,trade_id DESC LIMIT 1",
                (*params, *first)).fetchone()
            volume = Fraction(dec(previous[0])) if previous else Fraction(0)
            rows = [dict(row) for row in db.execute(f"SELECT * FROM cycle_fills WHERE {where} "
                "AND (executed_at,trade_id)>=(?,?) ORDER BY executed_at,trade_id", (*params, *first))]
        estimated = Fraction(dec(old["estimated_volume"])) if old else Fraction(0)
        count = (old["trade_count"] if old else 0) + len(new_rows)
        estimated_count = old["estimated_trade_count"] if old else 0
        for row in new_rows:
            if row["time_source"] == "legacy_estimated":
                estimated += Fraction(dec(row["notional"]))
                estimated_count += 1
        for row in rows:
            volume += Fraction(dec(row["notional"]))
            running = wire(volume)
            if row["daily_volume"] != running:
                row["daily_volume"] = running
                db.execute("UPDATE cycle_fills SET daily_volume=? WHERE account_id=? AND symbol=? AND trade_id=?",
                           (row["daily_volume"], account_id, row["symbol"], row["trade_id"]))
                db.execute("UPDATE events SET message=? WHERE id=? AND account_id=? AND kind='cycle_fill'",
                           (event_message(row), row["event_id"], account_id))
        last = rows[-1]
        placeholders = ",".join("?" for _ in range(11))
        key = "account_id,utc_date,symbol"
        db.execute(f"""INSERT INTO {table} VALUES ({placeholders})
                    ON CONFLICT({key}) DO UPDATE SET volume=excluded.volume,trade_count=excluded.trade_count,
                    estimated_volume=excluded.estimated_volume,estimated_trade_count=excluded.estimated_trade_count,
                    latest_trade_at=excluded.latest_trade_at,latest_symbol=excluded.latest_symbol,
                    latest_trade_id=excluded.latest_trade_id,updated_at=excluded.updated_at""",
                   (account_id, date, wire(volume), count, wire(estimated), estimated_count,
                    last["executed_at"], last["symbol"], last["trade_id"], time.time(), symbol))

    def cycle_daily_volume(self, account_id, now=None, *, symbol=None, include_pending=False):
        account_identifier(account_id)
        self._cycle_volume_symbol(symbol)
        date, start, reset = utc_day(now)
        with self.connect() as db:
            # Fills and their pending reservation must come from one WAL snapshot:
            # a concurrent backfill cannot disappear between the two reads.
            if include_pending and not db.in_transaction:
                db.execute("BEGIN")
            rows = db.execute("SELECT * FROM cycle_symbol_volume_days WHERE account_id=? AND utc_date=?"
                              + (" AND symbol=?" if symbol is not None else ""),
                              (account_id, date) + (() if symbol is None else (symbol,))).fetchall()
            pending = self._cycle_pending_volume(db, account_id, start, symbol) if include_pending else {}
        row = None
        if rows:
            row = {key: sum(r[key] for r in rows) for key in ("trade_count", "estimated_trade_count")}
            row.update({key: wire(sum((Fraction(dec(r[key])) for r in rows), Fraction(0)))
                        for key in ("volume", "estimated_volume")})
            row["latest_trade_at"] = max(r["latest_trade_at"] for r in rows)
        return {**({"symbol": symbol} if symbol is not None else {}), "utc_date": date, "volume": row["volume"] if row else "0", "trade_count": row["trade_count"] if row else 0,
                "next_reset_at": reset, "estimated_volume": row["estimated_volume"] if row else "0",
                "estimated_trade_count": row["estimated_trade_count"] if row else 0,
                "latest_trade_at": row["latest_trade_at"] if row else None,
                "cumulative_order": "execution_time", "timezone": "UTC", **pending}

    @staticmethod
    def _cycle_pending_volume(db, account_id, start, symbol):
        """Reserve unrecorded terminal order amounts, without inventing trades.

        Cross-midnight unassigned fills are conservatively charged to today.
        Only the exchange's cumulative quote amount is used, never a rounded
        average price. Missing or contradictory evidence keeps capped opens shut.
        """
        rows = db.execute("""SELECT i.id,i.data FROM cycle_volume_sync v JOIN intents i ON i.id=v.intent_id
            WHERE v.account_id=? AND v.synced_at IS NULL AND v.status IN ('complete','aborted')
            AND i.account_id=v.account_id AND i.status IN ('complete','aborted')
            AND (v.created_at>=? OR v.completed_at>=? OR v.completed_at IS NULL)
            """ + (" AND json_extract(i.data,'$.symbol')=?" if symbol is not None else "")
            + " LIMIT 1001", (account_id, start, start) + (() if symbol is None else (symbol,))).fetchall()
        reserved, error = Fraction(0), None
        if len(rows) > 1000:
            return {"reserved_volume": "0", "quota_pending": True, "sync_pending": True,
                    "error": "循环待补账批次过多，等待后台同步后核对日额度"}
        for row in rows:
            try:
                intent = json.loads(row["data"])
                if intent.get("id") != row["id"] or intent.get("account_id") != account_id:
                    raise TradingError("循环补账批次身份无法核对")
                fills = {}
                for fill in db.execute("SELECT client_id,quantity,notional FROM cycle_fills WHERE intent_id=?", (row["id"],)):
                    totals = fills.setdefault(fill["client_id"], [Fraction(0), Fraction(0)])
                    totals[0] += Fraction(positive(fill["quantity"]))
                    totals[1] += Fraction(positive(fill["notional"]))
                amount = Fraction(0)
                for cid, (order, receipt, _) in order_bindings(intent).items():
                    quantity = receipt_quantity(order, receipt, require_terminal=True)
                    recorded_qty, recorded_quote = fills.get(cid, (Fraction(0), Fraction(0)))
                    if recorded_qty > quantity:
                        raise TradingError("循环已入账成交数量超过终态回执")
                    if recorded_qty == quantity:
                        continue
                    quote = receipt.get("cumQuote")
                    if not isinstance(quote, str):
                        raise TradingError("循环终态回执缺少累计成交金额，等待明细核对日额度")
                    remaining = Fraction(positive(quote)) - recorded_quote
                    if remaining <= 0:
                        raise TradingError("循环终态回执金额与已入账成交冲突")
                    # All unrecorded amount is reserved on the current day. This
                    # also covers orders whose fills span midnight until the
                    # actual trade timestamps arrive and release the excess.
                    amount += remaining
                reserved += amount
            except (TradingError, KeyError, ValueError, TypeError) as exc:
                error = str(exc)
        return {"reserved_volume": wire(reserved), "quota_pending": error is not None,
                "sync_pending": bool(rows), "error": error}

    @staticmethod
    def _cycle_volume_symbol(symbol):
        if symbol is not None and (not isinstance(symbol, str) or symbol not in SYMBOLS):
            raise TradingError("循环成交统计品种无效")

    def cycle_rolling_volume(self, account_id, now=None, *, symbol=None):
        """Sum every fill in (now - 24h, now], independently of UTC day changes."""
        account_identifier(account_id)
        self._cycle_volume_symbol(symbol)
        end = time.time() if now is None else now
        if type(end) not in (int, float) or not 0 <= end <= 253402300799 or not math.isfinite(end):
            raise TradingError("循环滚动成交统计时间必须为有效时间戳")
        end = float(end)
        start = end - 86400
        volume, estimated = Fraction(0), Fraction(0)
        count, estimated_count, next_release = 0, 0, None
        with self.connect() as db:
            if not db.in_transaction:
                db.execute("BEGIN")
            revision = self._fill_revision(db, account_id)
            key = account_id, symbol
            cached = self._rolling_cache.read(key, revision, end)
            if cached is not None:
                return {**cached, "window_start": start, "window_end": end}
            # The account/time range uses idx_cycle_fills_recent. Iterate every
            # matching fill without a UI limit or lossy SQLite numeric SUM.
            rows = db.execute("""SELECT notional,time_source,executed_at FROM cycle_fills
                WHERE account_id=? AND executed_at>? AND executed_at<=?"""
                + (" AND symbol=?" if symbol is not None else "") + " ORDER BY executed_at",
                (account_id, start, end) + (() if symbol is None else (symbol,)))
            for row in rows:
                notional = Fraction(dec(row["notional"]))
                volume += notional
                count += 1
                if next_release is None:
                    next_release = row["executed_at"] + 86400
                if row["time_source"] == "legacy_estimated":
                    estimated += notional
                    estimated_count += 1
            future = self._next_fill(db, account_id, end, symbol=symbol)
        result = {**({"symbol": symbol} if symbol is not None else {}), "window_start": start, "window_end": end, "volume": wire(volume), "trade_count": count,
                  "next_release_at": next_release, "estimated_volume": wire(estimated),
                  "estimated_trade_count": estimated_count}
        self._rolling_cache.save(key, revision, end, min(next_release or math.inf, future or math.inf), result)
        return result

    @staticmethod
    def _fill_revision(db, account_id):
        row = db.execute("SELECT revision FROM cycle_fill_versions WHERE account_id=?", (account_id,)).fetchone()
        return row[0] if row else 0

    def cycle_fill_revision(self, account_id):
        account_identifier(account_id)
        with self.connect() as db:
            return self._fill_revision(db, account_id)

    @staticmethod
    def _next_fill(db, account_id, now, *, symbol=None):
        return db.execute("SELECT MIN(executed_at) FROM cycle_fills WHERE account_id=? AND executed_at>?"
                          + (" AND symbol=?" if symbol is not None else ""),
                          (account_id, now) + (() if symbol is None else (symbol,))).fetchone()[0]

    def cycle_report_boundary(self, account_id, now):
        """Next clock-only change, including future fills already in the ledger."""
        account_identifier(account_id)
        _, _, midnight = utc_day(now)
        with self.connect() as db:
            future = self._next_fill(db, account_id, now)
            oldest = db.execute("SELECT MIN(executed_at) FROM cycle_fills "
                                "WHERE account_id=? AND executed_at>? AND executed_at<=?",
                                (account_id, now - 86400, now)).fetchone()[0]
        return min(midnight, future or math.inf, oldest + 86400 if oldest is not None else math.inf)

    def cycle_trade_records(self, account_id, limit=100):
        account_identifier(account_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise TradingError("循环成交明细条数必须为 1 至 1000 的整数")
        with self.connect() as db:
            rows = db.execute("SELECT * FROM cycle_fills WHERE account_id=? ORDER BY executed_at DESC,symbol DESC,trade_id DESC LIMIT ?",
                              (account_id, limit)).fetchall()
        fields = FILL_FIELDS | {"utc_date", "daily_volume", "intent_id", "phase"}
        return [{key: row[key] for key in fields} for row in rows]

    def cycle_cost_records(self, account_id, now=None, limit=100):
        """Read complete fill contexts for current totals and displayed trades."""
        account_identifier(account_id)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise TradingError("循环成本明细条数必须为 1 至 1000 的整数")
        end = time.time() if now is None else now
        utc_day(end)
        end = float(end)
        fields = sorted(FILL_FIELDS | {"account_id", "intent_id", "phase", "utc_date"})
        with self.connect() as db:
            # Select through the account/time index, then seek complete intents.
            # A counterpart or repair can predate the window or the UI cutoff;
            # unrelated older intents must not turn this into an account scan.
            rows = db.execute("""WITH selected_intents AS (
                SELECT intent_id FROM cycle_fills
                WHERE account_id=? AND executed_at>? AND executed_at<=?
                UNION
                SELECT intent_id FROM (
                    SELECT intent_id FROM cycle_fills WHERE account_id=?
                    ORDER BY executed_at DESC,symbol DESC,trade_id DESC LIMIT ?
                )
            ) SELECT """ + ",".join(fields) + """ FROM cycle_fills INDEXED BY idx_cycle_fills_order
                WHERE account_id=? AND intent_id IN (SELECT intent_id FROM selected_intents)
                ORDER BY executed_at,symbol,trade_id""",
                              (account_id, end - 86400, end, account_id, limit, account_id))
            return [dict(row) for row in rows]

    def cycle_volume_backlog(self, account_id, limit=100, since=None, *, symbol=None):
        account_identifier(account_id)
        self._cycle_volume_symbol(symbol)
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise TradingError("循环成交补记批次数必须为 1 至 1000 的整数")
        if since is None:
            since = utc_day()[1]
        else:
            utc_day(since)
        with self.connect() as db:
            rows = db.execute("""SELECT i.data FROM cycle_volume_sync v JOIN intents i ON i.id=v.intent_id
                WHERE v.account_id=? AND v.synced_at IS NULL AND v.status IN ('complete','aborted')
                AND i.account_id=v.account_id AND i.status IN ('complete','aborted')
                AND (v.created_at>=? OR v.completed_at>=? OR v.completed_at IS NULL)
                """ + (" AND json_extract(i.data,'$.symbol')=?" if symbol is not None else "")
                + " ORDER BY COALESCE(v.completed_at,v.created_at,0) DESC,v.intent_id DESC LIMIT ?",
                (account_id, since, since) + (() if symbol is None else (symbol,)) + (limit,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def mark_cycle_volume_synced(self, intent_id):
        identifier(intent_id, "循环批次标识")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status,data FROM intents WHERE id=?", (intent_id,)).fetchone()
            if row is None or row["status"] not in ("complete", "aborted"):
                raise TradingError("循环批次尚未结束，不能标记成交同步完成")
            intent = json.loads(row["data"])
            self._cycle_ledger_intent(db, intent)
            bindings = order_bindings(intent)
            for client_id, (order, receipt, _) in bindings.items():
                expected = receipt_quantity(order, receipt, require_terminal=True)
                actual = sum((Fraction(dec(row[0])) for row in db.execute(
                    "SELECT quantity FROM cycle_fills WHERE intent_id=? AND client_id=?", (intent_id, client_id))), Fraction(0))
                if actual != expected:
                    raise TradingError("循环逐笔成交尚未全部入账，不能标记同步完成")
            self._index_cycle_volume(db, intent)
            db.execute("UPDATE cycle_volume_sync SET synced_at=? WHERE intent_id=?", (time.time(), intent_id))

    def event(self, account_id, kind, message):
        with self.connect() as db:
            db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)", (account_id, kind, message, time.time()))

    def record_cycle_check(self, account_id, symbol, phase, message, diagnostic=None):
        """Update only the latest uninterrupted check for this account and stage."""
        account_identifier(account_id)
        if not isinstance(symbol, str) or symbol not in SYMBOLS or phase not in ("open", "close"):
            raise TradingError("循环检查品种或阶段无效")
        if not isinstance(message, str) or not message:
            raise TradingError("循环检查消息无效")
        metadata = {"symbol": symbol, "phase": phase, "count": 1, "first_at": 0, "last_at": 0}
        if diagnostic is not None:
            metadata["diagnostic"] = diagnostic
        try:
            metadata = _cycle_check_metadata(json.dumps(metadata, ensure_ascii=False, allow_nan=False))
        except (TypeError, ValueError, OverflowError, RecursionError):
            metadata = None
        if metadata is None:
            raise TradingError("循环检查诊断格式无效")
        with self.connect() as db:
            # The database lock, rather than a Store-instance lock alone, keeps
            # concurrent workers and restarted processes from losing a count.
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            if type(now) not in (int, float) or not math.isfinite(now) or not 0 <= now <= 253402300799:
                raise TradingError("循环检查时间无效")
            previous = db.execute("SELECT id,kind,cycle_check FROM events WHERE account_id=? ORDER BY id DESC LIMIT 1",
                                  (account_id,)).fetchone()
            last = _cycle_check_metadata(previous["cycle_check"]) if previous and previous["kind"] == "cycle_check" else None
            metadata.update(first_at=now, last_at=now)
            if last and last["symbol"] == symbol and last["phase"] == phase and now >= last["last_at"]:
                metadata.update(count=last["count"] + 1, first_at=last["first_at"])
                db.execute("UPDATE events SET message=?,created_at=?,cycle_check=? WHERE id=? AND account_id=?",
                           (message, now, dumps(metadata), previous["id"], account_id))
            else:
                db.execute("INSERT INTO events(account_id,kind,message,created_at,cycle_check) VALUES (?,?,?,?,?)",
                           (account_id, "cycle_check", message, now, dumps(metadata)))

    def events(self, limit=100, *, account_id=None):
        if account_id is not None:
            account_identifier(account_id)
        with self.connect() as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM events"
                    + (" WHERE account_id IN (?, '')" if account_id is not None else "")
                    + " ORDER BY created_at DESC,id DESC LIMIT ?",
                    (limit,) if account_id is None else (account_id, limit))]
        for row in rows:
            raw = row.pop("cycle_check", None)
            metadata = _cycle_check_metadata(raw) if row["kind"] == "cycle_check" else None
            if metadata is not None:
                row["cycle_check"] = metadata
        return rows

    def complete_leverage(self, intent, actual):
        """Persist confirmation and the first-add priority in the same transaction."""
        intent.update(status="complete", confirmed_leverage=actual)
        with self.connect() as db:
            row = db.execute("SELECT status FROM intents WHERE id=?", (intent["id"],)).fetchone()
            if row and row[0] == "complete":
                return
            db.execute("UPDATE intents SET status='complete',data=? WHERE id=?", (dumps(intent), intent["id"]))
            if intent.get("purpose") != "migration":
                key = f"open_after_leverage:{intent['account_id']}:{intent['symbol']}"
                db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data", (key, dumps(actual)))

    def complete_cycle(self, intent, progress):
        """Commit cycle ownership/timing and the terminal receipt together once."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT status FROM intents WHERE id=?", (intent["id"],)).fetchone()
            if row and row[0] in ("complete", "aborted"):
                return
            if row is None:
                raise TradingError("多空循环批次记录不存在")
            key = "cycle:" + intent["account_id"]
            saved = db.execute("SELECT data FROM kv WHERE key=?", (key,)).fetchone()
            if not saved or json.loads(saved[0]).get("run_id") != progress.get("run_id") or progress.get("run_id") != intent.get("run_id"):
                raise TradingError("多空循环记录与批次不一致，等待人工核对")
            status = "aborted" if intent.get("status") == "aborted" else "complete"
            intent.update(status=status, completed_at=time.time())
            progress = {**progress, "updated_at": time.time(), "active_batch": None}
            db.execute("UPDATE intents SET status=?,data=? WHERE id=?", (status, dumps(intent), intent["id"]))
            self._index_cycle_volume(db, intent)
            db.execute("UPDATE kv SET data=? WHERE key=?", (dumps(progress), key))
            db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)",
                       (intent["account_id"], "cycle", progress.get("reason", "多空循环批次已核对"), time.time()))

    def confirm_cycle_recovery(self, account, previous, progress, review):
        """Archive the acknowledged state and reset tracking atomically, without trading."""
        aid, now = account["id"], time.time()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT data FROM accounts WHERE id=?", (aid,)).fetchone()
            old = db.execute("SELECT data FROM kv WHERE key=?", ("cycle:" + aid,)).fetchone()
            pending = db.execute("SELECT 1 FROM intents WHERE account_id=? AND status NOT IN ('complete','aborted')", (aid,)).fetchone()
            if (not saved or self.account_defaults(json.loads(saved[0])) != account or not old
                    or json.loads(old[0]) != previous or pending or account["enabled"]):
                raise TradingError("账户或循环记录已变化，请重新核对")
            paused = {**account, "enabled": False}
            paused.pop("pause_reason", None)
            audit = {"account_id": aid, "confirmed_at": now, "previous": previous,
                     "previous_pause_reason": account.get("pause_reason"), "review": review, "next": progress}
            db.execute("INSERT INTO kv VALUES (?,?)", (f"cycle_recovery:{aid}:{progress['run_id']}", dumps(audit)))
            db.execute("UPDATE kv SET data=? WHERE key=?", (dumps(progress), "cycle:" + aid))
            db.execute("UPDATE accounts SET data=? WHERE id=?", (dumps(paused), aid))
            message = (f"已人工核对 {review['symbol']}：记录多/空 {review['expected']['LONG']}/{review['expected']['SHORT']}，"
                       f"实际多/空 {review['actual']['LONG']}/{review['actual']['SHORT']}；实际仓位转为原始持仓，"
                       "本轮跟踪已结束，账户保持暂停，等待手动启动")
            db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)", (aid, "control", message, now))

    def complete_migration(self, intent, result, snapshot_remaining):
        """Commit the four-leg ledger and migration totals exactly once."""
        with self.connect() as db:
            row = db.execute("SELECT status FROM intents WHERE id=?", (intent["id"],)).fetchone()
            if row and row[0] in ("complete", "aborted"):
                return
            key = "migration:" + intent["account_id"]
            row = db.execute("SELECT data FROM kv WHERE key=?", (key,)).fetchone()
            progress = json.loads(row[0]) if row else None
            if not progress or progress.get("run_id") != intent.get("run_id"):
                raise TradingError("迁移周期记录与批次不一致，等待人工核对")
            status = "aborted" if intent.get("status") == "aborted" else "complete"
            intent.update(status=status, result=result, completed_at=time.time())
            db.execute("UPDATE intents SET status=?,data=? WHERE id=?", (status, dumps(intent), intent["id"]))
            for side in ("LONG", "SHORT"):
                source = Fraction(positive(result["source_notional"][side], True))
                target = Fraction(positive(result["target_notional"][side], True))
                progress["migrated_notional"][side] = wire(Fraction(dec(progress["migrated_notional"][side])) + source)
                progress["cumulative_notional_delta"][side] = wire(Fraction(dec(progress["cumulative_notional_delta"][side])) + target - source)
            moved = any(dec(result["source_qty"][side]) > 0 for side in ("LONG", "SHORT"))
            remaining = {side: wire(positive(snapshot_remaining[side], True)) for side in ("LONG", "SHORT")}
            done = not any(dec(value) for value in remaining.values())
            progress.update(source_remaining_qty=remaining, updated_at=time.time(),
                            completed_batches=progress.get("completed_batches", 0) + int(moved),
                            phase="complete" if done else "waiting", target_symbol=intent["target_symbol"],
                            target_leverage=intent["target_leverage"], required_leverage=intent["target_leverage"], active_batch=None,
                            reason="XAU 多空仓位已全部迁出" if done else "本批迁移已核对，等待下一批" if moved else "本批目标新增已回退，等待重新评估")
            db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data", (key, dumps(progress)))
            db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                       ("post_fill_check:" + intent["account_id"], dumps({"kind": "migration", "symbol": intent["target_symbol"], "leverage": intent["target_leverage"]})))
            db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)",
                       (intent["account_id"], "migration", progress["reason"], time.time()))

    def complete_pair(self, intent, quantities):
        """Commit the acknowledgement and aggregate progress in one transaction."""
        with self.connect() as db:
            row = db.execute("SELECT status FROM intents WHERE id=?", (intent["id"],)).fetchone()
            if row and row[0] == "complete":
                return
            intent["status"] = "complete"
            db.execute("UPDATE intents SET status='complete',data=? WHERE id=?", (dumps(intent), intent["id"]))
            db.execute("DELETE FROM kv WHERE key=?", (f"open_after_leverage:{intent['account_id']}:{intent['symbol']}",))
            db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                       ("post_fill_check:" + intent["account_id"], dumps({"symbol": intent["symbol"], "leverage": intent.get("leverage")})))
            key = "campaign:" + intent["account_id"]
            row = db.execute("SELECT data FROM kv WHERE key=?", (key,)).fetchone()
            campaign = json.loads(row[0]) if row else {"id": intent["id"], "batches": [], "started_at": time.time()}
            campaign["batches"].append({"symbol": intent["symbol"], "quantities": quantities, "leverage": intent["leverage"]})
            campaign["last_fill_at"] = time.time()
            db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data", (key, dumps(campaign)))
            db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)", (
                intent["account_id"], "fill", f"{intent['symbol']} {intent['leverage']}x 本批成交已核对，多头增加 {quantities['long_qty']}，空头增加 {quantities['short_qty']}", time.time()))

    def abort_pair(self, intent):
        """Even a fully repaired batch may have reduced equity through fees."""
        completed = {**intent, "status": "aborted"}
        with self.connect() as db:
            row = db.execute("SELECT status FROM intents WHERE id=?", (intent["id"],)).fetchone()
            if row and row[0] in ("complete", "aborted"):
                return
            db.execute("UPDATE intents SET status='aborted',data=? WHERE id=?", (dumps(completed), intent["id"]))
            db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                       ("post_fill_check:" + intent["account_id"], dumps({"symbol": intent["symbol"], "leverage": intent.get("leverage")})))
        intent.update(completed)

    def finish_campaign(self, account, reason, ratio):
        from .models import dec
        with self.connect() as db:
            key = "campaign:" + account["id"]
            row = db.execute("SELECT data FROM kv WHERE key=?", (key,)).fetchone()
            if not row:
                return
            campaign = json.loads(row[0])
            totals = {}
            for batch in campaign["batches"]:
                entry = totals.setdefault(batch["symbol"], {"long_qty": dec(0), "short_qty": dec(0), "notional": dec(0), "leverage": batch["leverage"]})
                quantities = batch["quantities"]
                # Campaigns persisted by older versions have one matched quantity.
                for side in ("long_qty", "short_qty"):
                    entry[side] += dec(quantities.get(side, quantities.get("qty")))
                entry["notional"] += dec(quantities["notional"])
                entry["leverage"] = batch["leverage"]
            lines = [f"Aster 双向开仓完成{'（模拟）' if account['mode'] == 'paper' else ''}", f"账户：{account['name']}（{account['id']}）"]
            lines.extend(f"{symbol} · {v['leverage']}x · 本轮多头增加 {v['long_qty']}，空头增加 {v['short_qty']}，新增总名义金额 {v['notional']:,.2f} USD1" for symbol, v in totals.items())
            ratio_text = f"{dec(ratio) * 100:.2f}%" if ratio is not None else "无法计算（账户总权益不足）"
            lines.extend([f"结束原因：{reason}", f"USD1 保证金占用率（总占用保证金 / 总权益）：{ratio_text}", f"批次数：{len(campaign['batches'])}"])
            message = "\n".join(lines)
            # Simulated trading must never send external completion messages.
            if account["mode"] == "live":
                db.execute("INSERT OR IGNORE INTO outbox(id,message,due_at) VALUES (?,?,?)", (campaign["id"], message, time.time()))
            db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)", (account["id"], "complete", message, time.time()))
            db.execute("DELETE FROM kv WHERE key=?", (key,))

    @staticmethod
    def capacity_alert_key(symbol, leverage):
        if symbol not in SYMBOLS or type(leverage) is not int or leverage not in TIERS:
            raise TradingError("额度提醒市场或杠杆档位无效")
        return f"capacity_alert:{symbol}:{leverage}"

    def observe_capacity_alert(self, symbol, leverage, value, *, threshold, cooldown, identity, checked_at, now=None,
                               account_labels=()):
        """Atomically persist an independent threshold gate and its one pending message."""
        key = self.capacity_alert_key(symbol, leverage)
        value, threshold = positive(value, True), positive(threshold, True)
        cooldown = float(positive(cooldown, True))
        now = time.time() if now is None else float(positive(now, True))
        checked_at = float(positive(checked_at, True))
        if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{64}", identity):
            raise TradingError("额度提醒配置标识无效")
        if not all(math.isfinite(v) for v in (now, checked_at, cooldown)):
            raise TradingError("额度提醒时间无效")
        if not -1 <= now - checked_at < CAPACITY_ALERT_MAX_AGE:
            return False
        try:
            checked_text = datetime.fromtimestamp(checked_at, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            raise TradingError("额度提醒检查时间无效") from None
        above = value > threshold
        account_text = "满足网页额度阈值的账户：" + "、".join(account_labels) + "\n" if account_labels else ""
        message = (f"Aster 开仓额度提醒\n{symbol} · {leverage}x\n"
                   f"公开剩余可开额度（估算）：{value:,.2f} USD1\n"
                   f"触发条件：> {threshold:,.2f} USD1\n"
                   f"{account_text}"
                   "未扣除个人持仓和挂单占用，请以账户页面为准。\n"
                   f"检查时间：{checked_text}\n"
                   f"https://www.asterdex.com/zh-CN/trade/pro/futures/{symbol}")
        queued = False
        with self.connect() as db:
            # Serialize read-modify-write across separate Store instances/processes.
            if not db.in_transaction:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT data FROM kv WHERE key=?", (key,)).fetchone()
            gate = json.loads(row[0]) if row else None
            if not gate or gate.get("identity") != identity:
                if gate and gate.get("pending_id"):
                    db.execute("UPDATE outbox SET expires_at=0 WHERE id=? AND delivered_at IS NULL", (gate["pending_id"],))
                gate = {"identity": identity, "notified": False, "last_alert": None, "pending_id": None,
                        "above": False, "fall_generation": 0}
            elif checked_at < gate.get("checked_at", 0):
                return False
            gate.setdefault("fall_generation", 0)
            if gate.get("above") and not above:
                gate["fall_generation"] += 1
            gate.update(above=above, checked_at=checked_at)
            pending_id = gate.get("pending_id")
            pending = db.execute("SELECT delivered_at FROM outbox WHERE id=?", (pending_id,)).fetchone() if pending_id else None
            if not pending or pending["delivered_at"] is not None:
                pending_id = gate["pending_id"] = None
            if not above:
                gate["notified"] = False
                if pending_id:
                    # Keep an in-flight id attached to the gate until its result is known.
                    db.execute("UPDATE outbox SET expires_at=0 WHERE id=?", (pending_id,))
            elif pending_id:
                # Refresh the estimate without resetting a failed send's retry backoff.
                db.execute("UPDATE outbox SET message=?,expires_at=? WHERE id=?",
                           (message, checked_at + CAPACITY_ALERT_MAX_AGE, pending_id))
            elif not gate.get("notified") and (gate.get("last_alert") is None or now - gate["last_alert"] >= cooldown):
                pending_id = "capacity-" + uuid.uuid4().hex
                db.execute("INSERT INTO outbox(id,message,due_at,expires_at,capacity_key) VALUES (?,?,?,?,?)",
                           (pending_id, message, now, checked_at + CAPACITY_ALERT_MAX_AGE, key))
                gate["pending_id"], queued = pending_id, True
            db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data", (key, dumps(gate)))
        return queued

    def invalidate_capacity_alert(self, symbol, leverage=None):
        """Pause stale/disabled sources without treating a failed read as a threshold fall."""
        keys = [self.capacity_alert_key(symbol, tier) for tier in (TIERS if leverage is None else (leverage,))]
        with self.connect() as db:
            db.executemany("UPDATE outbox SET expires_at=0 WHERE capacity_key=? AND delivered_at IS NULL", ((key,) for key in keys))

    @staticmethod
    def _notification_item(db, row):
        item = dict(row)
        if item["capacity_key"]:
            if item["capacity_key"] not in CAPACITY_ALERT_KEYS:
                return None
            saved = db.execute("SELECT data FROM kv WHERE key=?", (item["capacity_key"],)).fetchone()
            gate = json.loads(saved[0]) if saved else None
            if not gate or gate.get("pending_id") != item["id"] or not gate.get("above"):
                return None
            item["capacity_identity"] = gate["identity"]
            item["capacity_generation"] = gate.get("fall_generation", 0)
        return item

    def due_notifications(self):
        with self.connect() as db:
            now = time.time()
            rows = db.execute("""SELECT * FROM outbox WHERE delivered_at IS NULL AND due_at<=?
                AND (expires_at IS NULL OR expires_at>?)
                ORDER BY (capacity_key IS NOT NULL),due_at,id LIMIT 5""", (now, now)).fetchall()
            return [item for row in rows if (item := self._notification_item(db, row)) is not None]

    def notification_for_delivery(self, notification_id):
        """Re-read just before sending: a queued estimate may have fallen or expired."""
        with self.connect() as db:
            now = time.time()
            row = db.execute("""SELECT * FROM outbox WHERE id=? AND delivered_at IS NULL AND due_at<=?
                AND (expires_at IS NULL OR expires_at>?)""", (notification_id, now, now)).fetchone()
            return self._notification_item(db, row) if row else None

    def notification_result(self, item, success):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM outbox WHERE id=?", (item["id"],)).fetchone()
            if not row or row["delivered_at"] is not None:
                return
            now = time.time()
            if success:
                db.execute("UPDATE outbox SET delivered_at=? WHERE id=?", (now, item["id"]))
                if row["capacity_key"]:
                    saved = db.execute("SELECT data FROM kv WHERE key=?", (row["capacity_key"],)).fetchone()
                    gate = json.loads(saved[0]) if saved else None
                    if gate and gate.get("pending_id") == item["id"] and gate.get("identity") == item.get("capacity_identity"):
                        # An in-flight success does not consume a newer threshold crossing.
                        current_generation = item.get("capacity_generation", 0) == gate.get("fall_generation", 0)
                        gate.update(last_alert=now, notified=bool(gate.get("above")) and current_generation, pending_id=None)
                        db.execute("UPDATE kv SET data=? WHERE key=?", (dumps(gate), row["capacity_key"]))
                        _, symbol, leverage = row["capacity_key"].split(":")
                        db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)",
                                   ("", "capacity", f"{symbol} {leverage}x 额度达标提醒已发送飞书", now))
            else:
                attempts = row["attempts"] + 1
                db.execute("UPDATE outbox SET attempts=?,due_at=? WHERE id=?", (attempts, now + min(3600, 5 * 2 ** min(attempts, 10)), item["id"]))

    def pending_notifications(self):
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM outbox WHERE delivered_at IS NULL AND (expires_at IS NULL OR expires_at>0)").fetchone()[0]


class _StoreSnapshot(Store):
    """Single-threaded reader sharing one read-only transaction."""
    def __init__(self, owner, db):
        self.path, self.db = owner.path, db
        self._rolling_cache = owner._rolling_cache

    @contextmanager
    def connect(self):
        yield self.db
