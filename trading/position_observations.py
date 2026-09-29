"""Read existing position evidence without fetching or blocking trading workers."""
from contextlib import ExitStack
from copy import deepcopy
from fractions import Fraction
import json
import math

from .exchange import LiveBroker
from .execution import Executor, TERMINAL
from .models import SYMBOLS, TradingError, positive, wire

ACTIVITY_PREFIX = "position_activity:"


def initialize(db):
    # Transactional counters catch batches which begin and finish between two
    # notification ticks, including execution paths that write SQL directly.
    # No historical scan, new table or execution-side notification call is needed.
    for action, condition in (("INSERT", "1"), ("UPDATE", "NEW.status != OLD.status")):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS position_intent_{action.lower()}
            AFTER {action} ON intents WHEN {condition}
            BEGIN
              INSERT INTO kv(key,data) VALUES ('{ACTIVITY_PREFIX}account:' || NEW.account_id,'1')
              ON CONFLICT(key) DO UPDATE SET data=CAST(CAST(kv.data AS INTEGER)+1 AS TEXT);
            END""")
    for action, condition in (("INSERT", "json_extract(NEW.data,'$.pending') IS NOT NULL"),
                              ("UPDATE", """json_extract(NEW.data,'$.pending.id') IS NOT json_extract(OLD.data,'$.pending.id')
                                OR (json_extract(NEW.data,'$.pending') IS NOT NULL
                                    AND json_extract(NEW.data,'$.phase') IS NOT json_extract(OLD.data,'$.phase'))""")):
        db.execute(f"""CREATE TRIGGER IF NOT EXISTS position_pair_{action.lower()}
            AFTER {action} ON kv WHEN NEW.key GLOB 'pair_runtime:*' AND ({condition})
            BEGIN
              INSERT INTO kv(key,data) VALUES ('{ACTIVITY_PREFIX}pair:' || substr(NEW.key,14),'1')
              ON CONFLICT(key) DO UPDATE SET data=CAST(CAST(kv.data AS INTEGER)+1 AS TEXT);
            END""")


def terminal(orders, receipts):
    if not orders:
        return False
    try:
        for order, receipt in zip(orders, receipts, strict=True):
            Executor.validate_receipt(order, receipt)
            if receipt["status"] not in TERMINAL:
                return False
    except (TradingError, ValueError, TypeError, KeyError):
        return False
    return True


def settled_attention(pending, phase=None):
    if not pending or (phase if phase is not None else pending.get("status")) != "attention":
        return False
    if "legs" in pending:
        legs = pending["legs"] + pending.get("repairs", [])
        return terminal([leg["order"] for leg in legs], [leg.get("receipt") for leg in legs])
    orders = pending.get("orders", []) + pending.get("repairs", [])
    return terminal(orders, [pending.get("receipts", {}).get(order.get("newClientOrderId")) for order in orders])


def quantity(snapshot, symbol, side):
    positions = snapshot.get("positions") if isinstance(snapshot, dict) else None
    if not isinstance(positions, list):
        return None
    matches = [p for p in positions if isinstance(p, dict) and p.get("symbol") == symbol and p.get("side") == side]
    return matches[0].get("qty") if len(matches) == 1 else None


def activity(store, scope):
    return str(store.get(ACTIVITY_PREFIX + scope, 0))


def acquire(stack, locks):
    for lock in locks:
        if not lock.acquire(blocking=False):
            return False
        stack.callback(lock.release)
    return True


def sample(row, snapshots, sides):
    row["sample_times"] = [s.get("timestamp") for s in snapshots]
    row["long_qty"], row["short_qty"] = [quantity(s, row["symbol"], side) for s, side in zip(snapshots, sides)]
    row["suppressed"] |= any(bool(s.get("open_orders")) for s in snapshots)


def collect(engine):
    accounts, pairs = engine.store.accounts(), engine.store.pairs()
    members = {p[key] for p in pairs for key in ("long_account_id", "short_account_id")}
    modes = {a["id"]: a["mode"] for a in accounts}
    prefixes = {a["id"]: a.get("env_prefix") for a in accounts}
    schedules = engine.scheduling(accounts)
    result = []
    for pair in pairs:
        aids = [pair["long_account_id"], pair["short_account_id"]]
        if any(modes.get(aid) != "live" for aid in aids):
            continue
        scope = "pair:" + pair["id"]
        bindings = {aid: prefixes[aid] for aid in aids}
        row = {"key": scope + ":" + ":".join(aids + [str(prefixes[aid]) for aid in aids]),
               "scope": scope, "bindings": bindings, "label": "配对组「" + pair["name"] + "」",
               "symbol": pair["symbol"], "activity": activity(engine.store, scope), "sample_times": [], "suppressed": False,
               "max_gap": 70, "attention": False}
        result.append(row)
        with ExitStack() as stack:
            if not acquire(stack, [engine.pairs.group_lock(pair["id"])] + [engine.account_lock(aid) for aid in sorted(aids)]):
                continue
            with engine.store.read_snapshot() as store:
                current = store.pair(pair["id"])
                if current != pair:
                    continue
                state = store.get("pair_runtime:" + pair["id"], {}) or {}
                pending = state.get("pending")
                row["activity"] = activity(store, scope)
                row["attention"] = settled_attention(pending, state.get("phase"))
                # Archived unknown orders cannot be inferred settled from holdings.
                row["suppressed"] = bool(state.get("recovery_watch") or (pending and not row["attention"]))
                snapshots = state.get("snapshots", {})
                snapshots = [snapshots.get("long") or {}, snapshots.get("short") or {}]
                row["sample_times"] = [s.get("timestamp") for s in snapshots]
                row["suppressed"] |= any(bool(s.get("open_orders")) for s in snapshots)
                try:
                    # Include unexpected reverse-side holdings instead of hiding
                    # them behind the group's intended A-long/B-short assignment.
                    row["long_qty"], row["short_qty"] = [wire(sum(
                        (Fraction(positive(quantity(s, row["symbol"], side), True)) for s in snapshots), Fraction()))
                        for side in ("LONG", "SHORT")]
                except TradingError:
                    row["long_qty"] = row["short_qty"] = None
    for account in accounts:
        aid = account["id"]
        if aid in members or account["mode"] != "live":
            continue
        scope = "account:" + aid
        interval = schedules.get(aid, {}).get("interval", 60)
        marker = activity(engine.store, scope)
        rows = [{"key": scope + ":" + str(prefixes[aid]) + ":" + symbol, "scope": scope, "bindings": {aid: prefixes[aid]},
                 "label": "账户「" + account["name"] + "」",
                 "symbol": symbol, "activity": marker, "sample_times": [], "suppressed": False,
                 "max_gap": min(180, max(20, 2 * interval + 10)), "attention": False} for symbol in SYMBOLS]
        result.extend(rows)
        with ExitStack() as stack:
            if not acquire(stack, [engine.account_lock(aid)]):
                continue
            with engine.store.read_snapshot() as store:
                if store.account(aid) != account or store.pair_for_account(aid):
                    continue
                pending, post_fill = store.intent(aid), store.get("post_fill_check:" + aid)
                marker = activity(store, scope)
                attention = settled_attention(pending)
                suppressed = bool(post_fill or (pending and not attention))
                with engine.lock:
                    candidates = [deepcopy(engine.views.get(aid, {}).get("snapshot") or {}),
                                  deepcopy(engine.display_snapshots.get(aid) or {})]
                    broker = engine.brokers.get(aid)
                if isinstance(broker, LiveBroker) and account.get("cycle", {}).get("enabled"):
                    try:
                        lease = broker.cycle_cache.lease([account["cycle"]["symbol"]])
                        # Serialize only position evidence; no dashboard reporting.
                        s = lease.snapshot
                        candidates.append({"timestamp": s.timestamp, "open_orders": s.open_orders,
                            "positions": [{"symbol": p.symbol, "side": p.side, "qty": str(p.qty)} for p in s.positions]})
                        lease.require_fresh()
                    except (TradingError, KeyError, TypeError, ValueError):
                        candidates = candidates[:2]
                for row in rows:
                    row.update(activity=marker, suppressed=suppressed, attention=attention)
                    # A cycle-only snapshot must not erase another symbol's evidence.
                    available = [s for s in candidates if type(s.get("timestamp")) in (float, int)
                                 and math.isfinite(s["timestamp"])
                                 and all(quantity(s, row["symbol"], side) is not None for side in ("LONG", "SHORT"))]
                    snapshot = max(available, key=lambda s: s.get("timestamp", 0), default={})
                    sample(row, [snapshot, snapshot], ["LONG", "SHORT"])
    return result


def current(db, row):
    """Recheck durable identity/activity after capture, in the outbox transaction."""
    scope = row["scope"]
    marker = db.execute("SELECT data FROM kv WHERE key=?", (ACTIVITY_PREFIX + scope,)).fetchone()
    if row["activity"] != str(json.loads(marker[0]) if marker else 0):
        return False
    kind, identity = scope.split(":", 1)
    table = "pairs" if kind == "pair" else "accounts"
    saved = db.execute(f"SELECT data FROM {table} WHERE id=?", (identity,)).fetchone()
    if not saved:
        return False
    for aid, prefix in row.get("bindings", {}).items():
        member = db.execute("SELECT data FROM accounts WHERE id=?", (aid,)).fetchone()
        if not member:
            return False
        member = json.loads(member[0])
        if member.get("mode") != "live" or member.get("env_prefix") != prefix:
            return False
    if kind == "pair":
        pair = json.loads(saved[0])
        aids = [pair["long_account_id"], pair["short_account_id"]]
        runtime = db.execute("SELECT data FROM kv WHERE key=?", ("pair_runtime:" + identity,)).fetchone()
        runtime = json.loads(runtime[0]) if runtime else {}
        if (runtime.get("recovery_watch") or (runtime.get("pending")
                and not settled_attention(runtime["pending"], runtime.get("phase")))):
            return False
        return (set(row["bindings"]) == set(aids)
                and row["key"] == scope + ":" + ":".join(aids + [str(row["bindings"][aid]) for aid in aids]))
    intent = db.execute("SELECT data FROM intents WHERE account_id=? AND status NOT IN ('complete','aborted')", (identity,)).fetchone()
    check = db.execute("SELECT data FROM kv WHERE key=?", ("post_fill_check:" + identity,)).fetchone()
    if (intent and not settled_attention(json.loads(intent[0]))) or (check and json.loads(check[0])):
        return False
    return not db.execute("SELECT 1 FROM pair_members WHERE account_id=?", (identity,)).fetchone()
