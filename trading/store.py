"""Durable execution intents and notification outbox, isolated per account."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import threading
import time


def dumps(value):
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


class Store:
    def __init__(self, path):
        self.path = Path(path)
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
                    due_at REAL NOT NULL, delivered_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_outbox_due ON outbox(due_at) WHERE delivered_at IS NULL;
                PRAGMA optimize;
            """)

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
                intent["account_id"], "fill", f"{intent['symbol']} {intent['leverage']}x 本批双向成交完成，每边 {quantities['qty']}", time.time()))

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
                entry = totals.setdefault(batch["symbol"], {"qty": dec(0), "notional": dec(0), "leverage": batch["leverage"]})
                entry["qty"] += dec(batch["quantities"]["qty"])
                entry["notional"] += dec(batch["quantities"]["notional"])
                entry["leverage"] = batch["leverage"]
            lines = [f"Aster 双向开仓完成{'（模拟）' if account['mode'] == 'paper' else ''}", f"账户：{account['name']}（{account['id']}）"]
            lines.extend(f"{symbol} · {v['leverage']}x · 本轮每边增加 {v['qty']}，双边名义金额 {v['notional']:,.2f} USD1" for symbol, v in totals.items())
            lines.extend([f"结束原因：{reason}", f"USD1 保证金占用率（总占用保证金 / 总权益）：{dec(ratio) * 100:.2f}%", f"批次数：{len(campaign['batches'])}"])
            message = "\n".join(lines)
            # Simulated trading must never send external completion messages.
            if account["mode"] == "live":
                db.execute("INSERT OR IGNORE INTO outbox(id,message,due_at) VALUES (?,?,?)", (campaign["id"], message, time.time()))
            db.execute("INSERT INTO events(account_id,kind,message,created_at) VALUES (?,?,?,?)", (account["id"], "complete", message, time.time()))
            db.execute("DELETE FROM kv WHERE key=?", (key,))

    def due_notifications(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute("SELECT * FROM outbox WHERE delivered_at IS NULL AND due_at<=? ORDER BY due_at LIMIT 5", (time.time(),))]

    def notification_result(self, item, success):
        with self.connect() as db:
            if success:
                db.execute("UPDATE outbox SET delivered_at=? WHERE id=?", (time.time(), item["id"]))
            else:
                attempts = item["attempts"] + 1
                db.execute("UPDATE outbox SET attempts=?,due_at=? WHERE id=?", (attempts, time.time() + min(3600, 5 * 2 ** min(attempts, 10)), item["id"]))

    def pending_notifications(self):
        with self.connect() as db:
            return db.execute("SELECT count(*) FROM outbox WHERE delivered_at IS NULL").fetchone()[0]
