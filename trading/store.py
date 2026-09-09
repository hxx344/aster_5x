"""Durable execution intents and notification outbox, isolated per account."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid

from .models import SYMBOLS, TIERS, TradingError, positive


CAPACITY_ALERT_MAX_AGE = 8


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


class Store:
    def __init__(self, path):
        # All aliases of one database must share its process lock and WAL files.
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
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
                PRAGMA optimize;
            """)
            # Existing trade notifications keep NULL expiry and remain deliverable.
            db.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in db.execute("PRAGMA table_info(outbox)")}
            for name, kind in (("expires_at", "REAL"), ("capacity_key", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE outbox ADD COLUMN {name} {kind}")

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

    def accounts(self):
        with self.connect() as db:
            return [json.loads(r[0]) for r in db.execute("SELECT data FROM accounts ORDER BY id")]

    def account(self, account_id):
        with self.connect() as db:
            row = db.execute("SELECT data FROM accounts WHERE id=?", (account_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_account(self, account):
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
            key = f"open_after_leverage:{intent['account_id']}:{intent['symbol']}"
            db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data", (key, dumps(actual)))

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
                       ("post_fill_check:" + intent["account_id"], dumps(True)))
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
                       ("post_fill_check:" + intent["account_id"], dumps(True)))
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

    def observe_capacity_alert(self, symbol, leverage, value, *, threshold, cooldown, identity, checked_at, now=None):
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
        message = (f"Aster 开仓额度提醒\n{symbol} · {leverage}x\n"
                   f"公开剩余可开额度（估算）：{value:,.2f} USD1\n"
                   f"触发条件：> {threshold:,.2f} USD1\n"
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
