"""Local hourly schedule; callers own the SQLite transaction and runtime policy."""
import json

from . import monitoring

KEY = "hourly_summary_schedule"
CATEGORY = "hourly_summary"
INTERVAL_SECONDS = 3600


def next_boundary(now):
    return (int(now // INTERVAL_SECONDS) + 1) * INTERVAL_SECONDS


def read(db):
    row = db.execute("SELECT data FROM kv WHERE key=?", (KEY,)).fetchone()
    return {"next_due_at": None, "last_sent_at": None, **(json.loads(row[0]) if row else {})}


def write(db, schedule):
    db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
               (KEY, json.dumps(schedule, separators=(",", ":"))))


def reset(db, now, enabled):
    schedule = read(db)
    schedule["next_due_at"] = next_boundary(now) if enabled else None
    db.execute("""UPDATE outbox SET expires_at=0 WHERE category=? AND delivered_at IS NULL
        AND (expires_at IS NULL OR expires_at>0)""", (CATEGORY,))
    write(db, schedule)
    return schedule


def due(db, now, available):
    config, schedule = monitoring.read(db), read(db)
    if not available or not monitoring.allowed(config, CATEGORY, []):
        if schedule["next_due_at"] is not None:
            reset(db, now, False)
        return None
    if schedule["next_due_at"] is None:
        reset(db, now, True)
        return None
    if now < schedule["next_due_at"]:
        return None
    # A late wakeup describes the current hour only, never missed historical hours.
    return {"next_due_at": schedule["next_due_at"], "revision": config["revision"],
            "bucket": int(now // INTERVAL_SECONDS)}


def enqueue(db, token, message, now):
    config, schedule = monitoring.read(db), read(db)
    if (not monitoring.allowed(config, CATEGORY, []) or config["revision"] != token["revision"]
            or schedule["next_due_at"] != token["next_due_at"]
            or schedule["next_due_at"] is None or now < schedule["next_due_at"]
            or int(now // INTERVAL_SECONDS) != token["bucket"]):
        return False
    boundary = next_boundary(now)
    db.execute("UPDATE outbox SET expires_at=0 WHERE category=? AND delivered_at IS NULL AND expires_at>0", (CATEGORY,))
    inserted = db.execute("""INSERT OR IGNORE INTO outbox(id,message,due_at,expires_at,category,symbols)
        VALUES (?,?,?,?,?,'[]')""", (f"hourly-summary:{token['bucket']}", message, now, boundary, CATEGORY)).rowcount
    schedule["next_due_at"] = boundary
    write(db, schedule)
    return bool(inserted)


def delivered(db, now):
    schedule = read(db)
    schedule["last_sent_at"] = max(schedule["last_sent_at"] or 0, now)
    write(db, schedule)


def status(db, now, available):
    schedule = read(db)
    active = available and monitoring.allowed(monitoring.read(db), CATEGORY, [])
    pending = active and db.execute("""SELECT 1 FROM outbox WHERE category=? AND delivered_at IS NULL
        AND expires_at>? LIMIT 1""", (CATEGORY, now)).fetchone() is not None
    return {"interval_seconds": INTERVAL_SECONDS,
            "next_due_at": schedule["next_due_at"] if active else None,
            "last_sent_at": schedule["last_sent_at"], "pending": bool(pending)}
