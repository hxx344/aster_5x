"""Persistent summary intervals; callers own the SQLite transaction and policy."""
import json

from . import monitoring

KEY = "hourly_summary_schedule"
CATEGORY = "hourly_summary"
INTERVAL_SECONDS = 3600


def interval_seconds(config):
    return config["hourly_summary_interval_minutes"] * 60


def read(db):
    row = db.execute("SELECT data FROM kv WHERE key=?", (KEY,)).fetchone()
    # Older installations keep their existing hourly phase on upgrade.
    return {"next_due_at": None, "last_sent_at": None, "generation": 0,
            "interval_seconds": INTERVAL_SECONDS, **(json.loads(row[0]) if row else {})}


def write(db, schedule):
    db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
               (KEY, json.dumps(schedule, separators=(",", ":"))))


def reset(db, now, enabled):
    schedule = read(db)
    interval = interval_seconds(monitoring.read(db))
    schedule.update(next_due_at=now + interval if enabled else None,
                    interval_seconds=interval, generation=schedule["generation"] + 1)
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
    if schedule["next_due_at"] is None or schedule["interval_seconds"] != interval_seconds(config):
        reset(db, now, True)
        return None
    if now < schedule["next_due_at"]:
        return None
    # Advance within the persisted phase, skipping missed cycles without a burst.
    interval = schedule["interval_seconds"]
    period_at = schedule["next_due_at"] + int((now - schedule["next_due_at"]) // interval) * interval
    return {"next_due_at": schedule["next_due_at"], "revision": config["revision"],
            "generation": schedule["generation"], "interval_seconds": interval, "period_at": period_at}


def enqueue(db, token, message, now):
    config, schedule = monitoring.read(db), read(db)
    if (not monitoring.allowed(config, CATEGORY, []) or config["revision"] != token["revision"]
            or schedule["next_due_at"] != token["next_due_at"]
            or schedule["next_due_at"] is None or now < schedule["next_due_at"]
            or schedule["generation"] != token["generation"]
            or interval_seconds(config) != token["interval_seconds"]
            or not token["period_at"] <= now < token["period_at"] + token["interval_seconds"]):
        return False
    boundary = token["period_at"] + token["interval_seconds"]
    db.execute("UPDATE outbox SET expires_at=0 WHERE category=? AND delivered_at IS NULL AND expires_at>0", (CATEGORY,))
    inserted = db.execute("""INSERT OR IGNORE INTO outbox(id,message,due_at,expires_at,category,symbols)
        VALUES (?,?,?,?,?,'[]')""", (f"scheduled-summary:{token['generation']}:{round(token['period_at'] * 1000)}",
                                   message, now, boundary, CATEGORY)).rowcount
    schedule["next_due_at"] = boundary
    write(db, schedule)
    return bool(inserted)


def delivered(db, now):
    schedule = read(db)
    schedule["last_sent_at"] = max(schedule["last_sent_at"] or 0, now)
    write(db, schedule)


def status(db, now, available):
    schedule = read(db)
    config = monitoring.read(db)
    active = available and monitoring.allowed(config, CATEGORY, [])
    pending = active and db.execute("""SELECT 1 FROM outbox WHERE category=? AND delivered_at IS NULL
        AND expires_at>? LIMIT 1""", (CATEGORY, now)).fetchone() is not None
    return {"interval_seconds": interval_seconds(config),
            "next_due_at": schedule["next_due_at"] if active else None,
            "last_sent_at": schedule["last_sent_at"], "pending": bool(pending)}
