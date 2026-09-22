"""Bounded, read-only portal summary from already published dashboard data."""
from datetime import datetime, timezone
import math
import time

from .report_cache import REPORT_MAX_AGE


STALE_AFTER_SECONDS = 120


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _sum(values):
    if not values or any(value is None for value in values):
        return None
    return _number(sum(values))


def hub_summary(engine, *, now=None):
    """Never start account reads, history calculations or report refresh workers."""
    now = time.time() if now is None else now
    with engine.store.read_snapshot() as reader:
        accounts = [account for account in reader.accounts() if account.get("enabled")]
    live = [account for account in accounts if account.get("mode") == "live"]
    stamps, margins, volumes = [], [], []
    with engine.lock:
        ready, error = engine.ready, engine.error
        for account in live:
            aid = account["id"]
            snapshot = engine.views.get(aid, {}).get("snapshot") or {}
            displayed = engine.display_snapshots.get(aid) or {}
            if (_number(displayed.get("timestamp")) or 0) > (_number(snapshot.get("timestamp")) or 0):
                snapshot = displayed
            stamp = _number(snapshot.get("timestamp"))
            stamps.append(stamp if stamp is not None and 0 < stamp <= now + 60 else None)
            margins.append(_number(snapshot.get("occupied_margin")))
    utc_date = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    reports = engine.dashboard_reports
    with reports.lock:
        for account in live:
            entry = reports.entries.get(account["id"], {})
            stamp = _number(entry.get("as_of"))
            valid = (entry.get("key") == reports.key(account) and not entry.get("error")
                     and stamp is not None and 0 <= now - stamp < REPORT_MAX_AGE
                     and now // 86400 == stamp // 86400)
            report = entry.get("data") or {}
            daily = report.get("volumes", {}).get(account["cycle"]["symbol"], {}).get("daily_volume", {})
            volumes.append(_number(daily.get("volume")) if valid and daily.get("utc_date") == utc_date else None)
    oldest = min(stamps) if stamps and all(stamp is not None for stamp in stamps) else None
    updated_at = (datetime.fromtimestamp(oldest, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                  if oldest is not None else None)
    partial = bool(error or not ready or any(value is None for value in [*stamps, *margins, *volumes]))
    offline = engine.shutdown.is_set() or (engine.thread is not None and not engine.thread.is_alive())
    health = "offline" if offline else "stale" if oldest is not None and now - oldest >= STALE_AFTER_SECONDS else "partial" if partial else "online"
    message = ("上游为演示模式；交易数据不计入资产汇总" if engine.demo
               else "交易服务已连接；保证金与成交量使用 USD1 口径")
    if health != "online":
        message += {"offline": "；交易服务已停止", "stale": "；账户快照已过期", "partial": "；部分数据尚未就绪"}[health]
    metrics = [
        {"key": "accounts", "label": "启用账户", "value": len(accounts), "unit": "个"},
        {"key": "live_accounts", "label": "实盘账户", "value": len(live), "unit": "个"},
        {"key": "occupied_margin", "label": "实盘占用保证金", "value": _sum(margins), "unit": "USD1"},
        {"key": "daily_volume", "label": "实盘今日成交量", "value": _sum(volumes), "unit": "USD1",
         "detail": "上游 UTC 日口径；不计入资产汇总"},
    ]
    return {"schemaVersion": 2, "data": {"updatedAt": updated_at,
            "health": {"state": health, "message": message, "staleAfterSeconds": STALE_AFTER_SECONDS}, "metrics": metrics}}
