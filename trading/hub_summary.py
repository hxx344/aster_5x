"""Bounded, read-only portal summary from already published dashboard data."""
from datetime import date, datetime, timezone
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


def _text(value, fallback):
    return value.strip()[:120] if isinstance(value, str) and value.strip() else fallback


def _message(base, reasons):
    message = base
    for index, reason in enumerate(reasons):
        candidate = f"{message}；{reason}"
        # The portal validates JavaScript string length (UTF-16 code units).
        if len(candidate.encode("utf-16-le")) // 2 + 40 > 500:
            return f"{message}；另有 {len(reasons) - index} 项异常，请进入项目查看"
        message = candidate
    return message


def _snapshot(now, *candidates):
    candidates = [value for value in candidates if isinstance(value, dict) and value]
    valid = [(stamp, value) for value in candidates
             if (stamp := _number(value.get("timestamp"))) is not None and 0 < stamp <= now + 60]
    if valid:
        stamp, snapshot = max(valid, key=lambda item: item[0])
        return snapshot, stamp
    return (candidates[0] if candidates else {}), None


def _pair_orders_unresolved(runtime):
    if (runtime.get("recovery_watch") is not None or runtime.get("attention")
            or runtime.get("phase") in ("attention", "reconciling", "repairing")):
        return True
    pending = runtime.get("pending")
    if pending is None:
        return False
    if not isinstance(pending, dict):
        return True
    if pending.get("kind") == "leverage":
        results = pending.get("results")
        return (runtime.get("phase") != "leverage" or not isinstance(results, dict)
                or any(not isinstance(row, dict) or row.get("unknown") for row in results.values()))
    if (runtime.get("phase") != "submitting" or pending.get("kind") not in ("cycle", "ordinary")
            or pending.get("phase") not in ("open", "close")):
        return True
    legs, repairs = pending.get("legs"), pending.get("repairs")
    if not isinstance(legs, list) or len(legs) != 2 or not isinstance(repairs, list) or repairs:
        return True
    for leg in legs:
        if not isinstance(leg, dict):
            return True
        receipt = leg.get("receipt")
        if receipt is None:
            # Sending is normal; a returned error/evidence without a receipt
            # proves uncertainty, even before phase becomes reconciling.
            if leg.get("error") or leg.get("submit_error") or leg.get("submit_evidence"):
                return True
        elif not isinstance(receipt, dict) or receipt.get("status") not in (
                "NEW", "PARTIALLY_FILLED", "PENDING_CANCEL", "FILLED", "CANCELED",
                "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"):
            return True
    return False


def _pair_volume(runtime, side, now, utc_date):
    if not isinstance(runtime, dict) or not runtime:
        return None, "配对运行记录缺失或无效"
    stamp = _number(runtime.get("updated_at"))
    if stamp is None or not 0 < stamp <= now:
        return None, "配对成交统计缺少有效更新时间"
    if now - stamp >= STALE_AFTER_SECONDS:
        return None, f"配对成交统计已过期（超过 {STALE_AFTER_SECONDS} 秒）"
    if _pair_orders_unresolved(runtime):
        return None, "配对订单尚待核对，今日成交量暂不可用"
    unknown = bool(runtime.get("volume_unknown"))
    until = runtime.get("volume_unknown_until_utc")
    if until is not None:
        try:
            if not isinstance(until, str) or date.fromisoformat(until).isoformat() != until:
                raise ValueError
            unknown = utc_date <= until
        except ValueError:
            return None, "配对成交统计未知日期无效"
    if unknown:
        return None, "配对今日成交量存在未知成交"
    daily = runtime.get("daily_volume")
    if not isinstance(daily, dict):
        return None, "配对今日成交量缺失或无效"
    # The ledger creates a day only after a batch is reconciled. In-flight
    # normal batches do not invalidate the already confirmed daily total.
    if utc_date not in daily:
        return 0.0, None
    row = daily[utc_date]
    value = _number(row.get(side)) if isinstance(row, dict) else None
    if value is None or value < 0:
        return None, "配对今日成交量缺失或无效"
    return value, None


def hub_summary(engine, *, now=None):
    """Never start account reads, history calculations or report refresh workers."""
    now = time.time() if now is None else now
    with engine.store.read_snapshot() as reader:
        saved_accounts = reader.accounts()
        pairs = [pair for pair in reader.pairs() if pair.get("enabled")]
        paired = {}
        for pair in pairs:
            runtime = reader.get("pair_runtime:" + pair["id"])
            for side in ("long", "short"):
                paired[pair[side + "_account_id"]] = (runtime, side)
        accounts = {account["id"]: account for account in saved_accounts
                    if account.get("enabled") or account["id"] in paired}
    accounts = list(accounts.values())
    live = [account for account in accounts if account.get("mode") == "live"]
    stamps, margins, volumes, reasons = [], [], [], []
    with engine.lock:
        ready, error = engine.ready, engine.error
        if error:
            reasons.append(f"交易服务异常：{_text(error, '上游未提供具体原因')}")
        if not ready:
            reasons.append("交易服务尚未就绪")
        for account in live:
            aid = account["id"]
            label = f"{_text(account.get('name'), '实盘账户')}（{_text(aid, '未知账户')}）"
            runtime, side = paired.get(aid, (None, None))
            snapshots = runtime.get("snapshots") if isinstance(runtime, dict) else None
            snapshot, stamp = _snapshot(now,
                snapshots.get(side) if isinstance(snapshots, dict) else None,
                engine.views.get(aid, {}).get("snapshot"), engine.display_snapshots.get(aid))
            stamps.append(stamp)
            margins.append(_number(snapshot.get("occupied_margin")))
            if not snapshot:
                reasons.append(f"{label}：缺少账户快照")
            else:
                if stamps[-1] is None:
                    reasons.append(f"{label}：快照缺少有效更新时间")
                elif now - stamp >= STALE_AFTER_SECONDS:
                    reasons.append(f"{label}：账户快照已过期（超过 {STALE_AFTER_SECONDS} 秒）")
                if margins[-1] is None:
                    reasons.append(f"{label}：保证金数据缺失或无效")
    utc_date = datetime.fromtimestamp(now, timezone.utc).date().isoformat()
    reports = engine.dashboard_reports
    with reports.lock:
        for account in live:
            if account["id"] in paired:
                runtime, side = paired[account["id"]]
                volume, reason = _pair_volume(runtime, side, now, utc_date)
                volumes.append(volume)
                if reason:
                    label = f"{_text(account.get('name'), '实盘账户')}（{_text(account['id'], '未知账户')}）"
                    reasons.append(f"{label}：{reason}")
                continue
            entry = reports.entries.get(account["id"], {})
            stamp = _number(entry.get("as_of"))
            valid = (entry.get("key") == reports.key(account) and not entry.get("error")
                     and stamp is not None and 0 <= now - stamp < REPORT_MAX_AGE
                     and now // 86400 == stamp // 86400)
            report = entry.get("data") or {}
            daily = report.get("volumes", {}).get(account["cycle"]["symbol"], {}).get("daily_volume", {})
            volumes.append(_number(daily.get("volume")) if valid and daily.get("utc_date") == utc_date else None)
            if volumes[-1] is None:
                label = f"{_text(account.get('name'), '实盘账户')}（{_text(account['id'], '未知账户')}）"
                if entry.get("error"):
                    reason = f"成交统计读取失败：{_text(entry['error'], '上游未提供具体原因')}"
                elif not entry or not report:
                    reason = "成交统计尚未生成"
                elif entry.get("key") != reports.key(account):
                    reason = "账户配置已变更，成交统计等待重新生成"
                elif stamp is None or stamp > now:
                    reason = "成交统计缺少有效更新时间"
                elif now // 86400 != stamp // 86400 or daily.get("utc_date") != utc_date:
                    reason = "成交统计缺少当日 UTC 数据"
                elif now - stamp >= REPORT_MAX_AGE:
                    reason = f"成交统计已过期（超过 {REPORT_MAX_AGE} 秒）"
                else:
                    reason = "今日成交量缺失或无效"
                reasons.append(f"{label}：{reason}")
    oldest = min(stamps) if stamps and all(stamp is not None for stamp in stamps) else None
    updated_at = (datetime.fromtimestamp(oldest, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                  if oldest is not None else None)
    partial = bool(error or not ready or any(value is None for value in [*stamps, *margins, *volumes]))
    offline = engine.shutdown.is_set() or (engine.thread is not None and not engine.thread.is_alive())
    health = "offline" if offline else "stale" if oldest is not None and now - oldest >= STALE_AFTER_SECONDS else "partial" if partial else "online"
    if offline:
        reasons.insert(0, "交易服务已停止")
    message = _message("上游为演示模式；交易数据不计入资产汇总" if engine.demo
                       else "交易服务已连接；保证金与成交量使用 USD1 口径" if health == "online"
                       else "保证金与成交量使用 USD1 口径", reasons)
    metrics = [
        {"key": "accounts", "label": "启用账户", "value": len(accounts), "unit": "个"},
        {"key": "live_accounts", "label": "实盘账户", "value": len(live), "unit": "个"},
        {"key": "occupied_margin", "label": "实盘占用保证金", "value": _sum(margins), "unit": "USD1"},
        {"key": "daily_volume", "label": "实盘今日成交量", "value": _sum(volumes), "unit": "USD1",
         "detail": "上游 UTC 日口径；配对仅统计已核对成交；不计入资产汇总" if any(account["id"] in paired for account in live)
                   else "上游 UTC 日口径；不计入资产汇总"},
    ]
    return {"schemaVersion": 2, "data": {"updatedAt": updated_at,
            "health": {"state": health, "message": message, "staleAfterSeconds": STALE_AFTER_SECONDS}, "metrics": metrics}}
