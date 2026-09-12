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


CAPACITY_ALERT_MAX_AGE = 8
CAPACITY_ALERT_KEYS = frozenset(f"capacity_alert:{symbol}:{tier}" for symbol in SYMBOLS for tier in TIERS)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


class Store:
    def __init__(self, path, *, demo=None):
        # All aliases of one database must share its process lock and WAL files.
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            # Decide HTTP exposure before touching historical schema or state.
            # Keep the claim and migrations in one rollback-capable transaction;
            # executescript would implicitly commit the claim before migrating.
            db.execute("BEGIN IMMEDIATE")
            if demo is not None:
                self._bind_runtime_mode(db, demo=demo)
            schema = """
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, data TEXT NOT NULL
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
                    kind TEXT NOT NULL, message TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_account ON events(account_id,id);
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, message TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    due_at REAL NOT NULL, delivered_at REAL, expires_at REAL, capacity_key TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(due_at) WHERE delivered_at IS NULL;
            """
            for statement in schema.split(";"):
                if statement.strip():
                    db.execute(statement)
            # Existing trade notifications keep NULL expiry and remain deliverable.
            columns = {row[1] for row in db.execute("PRAGMA table_info(outbox)")}
            for name, kind in (("expires_at", "REAL"), ("capacity_key", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE outbox ADD COLUMN {name} {kind}")
            for row in db.execute("SELECT id,data FROM accounts").fetchall():
                account = json.loads(row["data"])
                normalized = self.account_defaults(account)
                if normalized != account:
                    db.execute("UPDATE accounts SET data=? WHERE id=?", (dumps(normalized), row["id"]))
            placeholders = ",".join("?" for _ in CAPACITY_ALERT_KEYS)
            db.execute(f"""UPDATE outbox SET expires_at=0 WHERE delivered_at IS NULL
                AND capacity_key IS NOT NULL AND capacity_key NOT IN ({placeholders})""", tuple(CAPACITY_ALERT_KEYS))
        # WAL cannot be enabled inside the initialization transaction. A rejected
        # mode or failed migration never reaches these database-level changes.
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA optimize")

    @contextmanager
    def connect(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=10)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA synchronous=FULL")
            try:
                with db:
                    yield db
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
            expected = {"accounts", "kv", "intents", "events", "outbox"}
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
        policy = account.get("policy")
        if isinstance(policy, dict):
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
            db.execute("INSERT INTO accounts VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data", (account["id"], dumps(account)))

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

    def save_intent(self, intent):
        with self.connect() as db:
            db.execute("INSERT INTO intents VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,data=excluded.data",
                       (intent["id"], intent["account_id"], intent["status"], dumps(intent)))

    def event(self, account_id, kind, message):
        with self.connect() as db:
            db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)", (account_id, kind, message, time.time()))

    def events(self, limit=100):
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]

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
