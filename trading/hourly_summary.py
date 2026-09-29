"""Compact, native Feishu cards from published state; no exchange/database I/O."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_UP
import json

from monitor import FEISHU_CARD_PREFIX
from .report_cache import REPORT_MAX_AGE


# Budget the HTTP serialization, leaving room below the custom bot's 20 KB limit.
MAX_MESSAGE_BYTES = 18000
DISPLAY_ZONE = timezone(timedelta(hours=8))
PHASES = {
    "starting": "启动中", "waiting": "等待条件", "paused": "已暂停",
    "attention": "需要处理", "error": "异常", "reconciling": "核对中",
    "waiting_open": "等待开仓", "waiting_close": "等待平仓", "holding": "持仓中",
    "daily_limit": "当日目标已达", "complete": "已完成", "disabled": "未启用",
    "unknown": "结果未知", "accepted": "待核对", "acknowledged": "待刷新",
    "blocked": "已阻止", "cooldown": "冷却中", "confirmed": "已核实",
    "paper_confirmed": "模拟已核实", "rejected": "已拒绝", "submitting": "提交中",
}
ATTENTION = {"attention", "error", "reconciling", "unknown", "blocked", "rejected"}


def _map(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _text(value, fallback="待同步", limit=64):
    return " ".join(value.split())[:limit] if isinstance(value, str) and value.strip() else fallback


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
        return number if number.is_finite() and abs(number) < Decimal("1e24") else None
    except (ValueError, TypeError, InvalidOperation):
        return None


def _amount(value, places=2):
    number = _number(value)
    return f"{number:,.{places}f}" if number is not None else "待同步"


def _phase(value):
    return PHASES.get(value, _text(value)) if isinstance(value, str) else "待同步"


def _fresh(stamp, now, max_age):
    stamp = _number(stamp)
    return stamp is not None and 0 <= Decimal(str(now)) - stamp < max_age


def _label(content, size="normal", color="default"):
    # Dynamic names/reasons are plain text, never links, HTML or @ mentions.
    return {"tag": "div", "text": {"tag": "plain_text", "content": content,
            "text_size": size, "text_color": color}}


def _columns(*columns, background="default", padding="0px"):
    return {"tag": "column_set", "flex_mode": "bisect" if len(columns) == 2 else "none",
            "horizontal_spacing": "12px", "background_style": background,
            "columns": [{"tag": "column", "width": "weighted", "weight": 1,
                         "vertical_align": "top", "vertical_spacing": "4px",
                         "padding": padding, "elements": elements} for elements in columns]}


def _metric(title, value, size="heading-2", color="default"):
    return [_label(title, "notation", "grey"), _label(value, size, color)]


def _progress(value, target, known):
    used, limit = _number(value), _number(target)
    if not known or used is None or used < 0:
        return "待同步 / 待核实"
    if limit is None or limit < 0:
        return "目标待同步"
    if limit == 0:
        return "未设目标上限"
    if used >= limit:
        return "已达标"
    # Never round a positive remaining amount to zero or label 99.998% as 100%.
    remaining = (limit - used).quantize(Decimal("0.01"), rounding=ROUND_UP)
    return f"还差 {remaining:,.2f}"


def _usage(snapshot):
    equity, occupied = _number(snapshot.get("equity")), _number(snapshot.get("occupied_margin"))
    if equity is None or equity <= 0 or occupied is None or occupied < 0:
        return "待同步"
    return f"{occupied / equity * 100:.2f}%"


def _latest_snapshot(account, saved=None):
    saved, published = _map(saved), _map(account.get("snapshot"))
    saved_stamp, published_stamp = _number(saved.get("timestamp")), _number(published.get("timestamp"))
    return published if published_stamp is not None and (saved_stamp is None or published_stamp > saved_stamp) else saved or published


def _snapshot_note(snapshot, now):
    stamp = _number(snapshot.get("timestamp"))
    if stamp is None or stamp > now:
        return "账户数据待同步"
    return f"账户数据 {now - float(stamp):.0f} 秒前 · 待刷新" if not _fresh(stamp, now, 8) else None


def _account_card(account, snapshot, label, progress, now, side=None):
    mode = {"live": "实盘", "paper": "模拟"}.get(account.get("mode"), "模式待同步")
    color = "blue" if side == "long" else "red" if side == "short" else "default"
    elements = [_label(f"{label} · {_text(account.get('name'))}（{mode}）", "heading", color),
                _columns(_metric("可用保证金", _amount(snapshot.get("available"))),
                         _metric("保证金占用率", _usage(snapshot))),
                _label(f"距交易量目标  {progress}")]
    note = _snapshot_note(snapshot, now)
    if note:
        elements.append(_label(note, "notation", "orange"))
    background = "rgba(51,112,255,0.06)" if side != "short" else "rgba(245,74,69,0.06)"
    return _columns(elements, background=background, padding="12px")


def _position_text(snapshot, symbol=None, side=None):
    if not isinstance(snapshot.get("positions"), list):
        return "待同步"
    rows = [p for p in _rows(snapshot["positions"]) if (not symbol or p.get("symbol") == symbol)
            and (not side or p.get("side") == side)]
    values = [_number(p.get("qty")) for p in rows]
    if any(value is None or value < 0 for value in values):
        return "待同步"
    if side:
        return f"{sum(values, Decimal(0)):,.6f}".rstrip("0").rstrip(".")
    held = [(p, value) for p, value in zip(rows, values) if value]
    if not held:
        return "空仓"
    return "；".join(f"{_text(p.get('symbol'), limit=16)} {'多' if p.get('side') == 'LONG' else '空' if p.get('side') == 'SHORT' else '?'} {_amount(value, 3)}"
                     for p, value in held[:2]) + (f"；另 {len(held)-2} 项" if len(held) > 2 else "")


def _capacity(pair, snapshots, markets, now):
    symbol = pair.get("symbol")
    leverages = {_number(p.get("leverage")) for snapshot in snapshots.values()
                 for p in _rows(snapshot.get("positions")) if p.get("symbol") == symbol}
    if len(leverages) != 1 or None in leverages:
        return "公开额度待同步"
    leverage = next(iter(leverages))
    if leverage <= 0 or leverage != int(leverage):
        return "公开额度待同步"
    key = str(int(leverage))
    market = _map(markets.get(symbol))
    value = _number(_map(market.get("capacities")).get(key))
    stamp = _map(market.get("capacity_checked_at")).get(key)
    if value is None or value < 0 or market.get("error") or not _fresh(stamp, now, 8):
        return f"公开额度 {key}x 待同步"
    return f"公开额度 {key}x  {_amount(value)}"


def _pair_block(pair, accounts, markets, now, utc_date):
    state, config = _map(pair.get("state")), _map(pair.get("cycle"))
    margin = _map(state.get("margin"))
    transfer = _map(margin.get("pending"))
    urgent = (state.get("phase") in ATTENTION or bool(pair.get("pause_reason"))
              or bool(state.get("pending")) or bool(state.get("volume_unknown")) or bool(transfer)
              or margin.get("status") in ATTENTION)
    phase = "已暂停" if not pair.get("enabled") else _phase(state.get("phase"))
    reason = _text(pair.get("pause_reason") or state.get("reason"), "")
    if (pair.get("enabled") and state.get("phase") in {"waiting", "waiting_open"}
            and any(word in reason for word in ("公开额度", "公开可用额度"))):
        phase = "等待公开额度"
    elements = [_label(f"{_text(pair.get('name'))} · {_text(pair.get('symbol'))}  |  {phase}", "heading")]
    if urgent and reason:
        elements.append(_label(reason, color="orange"))
    if state.get("pending"):
        elements.append(_label("订单 / 执行批次待核对", color="orange"))
    daily = _map(_map(state.get("daily_volume")).get(utc_date))
    known = not state.get("volume_unknown") and not state.get("pending") and _fresh(state.get("updated_at"), now, 120)
    snapshots = {}
    for side, label in (("long", "A 多"), ("short", "B 空")):
        account = accounts.get(pair.get(side + "_account_id"), {})
        snapshot = _latest_snapshot(account, _map(state.get("snapshots")).get(side))
        snapshots[side] = snapshot
        progress = _progress(daily.get(side), config.get("daily_volume_limit"), known) if config.get("enabled") else "循环未启用"
        elements.append(_account_card(account, snapshot, label, progress, now, side))
    positions = f"持仓  多 {_position_text(snapshots['long'], pair.get('symbol'), 'LONG')} / 空 {_position_text(snapshots['short'], pair.get('symbol'), 'SHORT')}"
    elements.append(_label(positions, "notation"))
    elements.append(_label(f"{_capacity(pair, snapshots, markets, now)} · 划转{_phase(margin.get('status'))}", "notation"))
    if transfer:
        directions = {"long": "A 多", "short": "B 空"}
        elements.append(_label(f"待核对划转 {directions.get(transfer.get('source'), '?')} → {directions.get(transfer.get('destination'), '?')}：{_amount(transfer.get('amount'))} USD1", color="orange"))
    elif margin.get("status") in ATTENTION and margin.get("reason"):
        elements.append(_label(_text(margin["reason"]), color="orange"))
    return urgent, elements


def _account_block(account, now, utc_date):
    cycle = _map(account.get("cycle_state"))
    daily, report = _map(cycle.get("daily_volume")), _map(cycle.get("report_status"))
    known = (report.get("status") == "ready" and _fresh(report.get("as_of"), now, REPORT_MAX_AGE)
             and daily.get("utc_date") == utc_date and not daily.get("sync_pending") and not daily.get("error"))
    progress = _progress(daily.get("volume"), _map(account.get("cycle")).get("daily_volume_limit"), known) if _map(account.get("cycle")).get("enabled") else "循环未启用"
    snapshot = _map(account.get("snapshot"))
    urgent = account.get("status") in ATTENTION or bool(account.get("pause_reason"))
    elements = [_account_card(account, snapshot, "账户", progress, now),
                _label(f"{'启用' if account.get('enabled') else '暂停'} · {_phase(account.get('status'))} · 持仓 {_position_text(snapshot)}", "notation")]
    if urgent and (account.get("pause_reason") or account.get("reason")):
        elements.append(_label(_text(account.get("pause_reason") or account.get("reason")), color="orange"))
    return urgent, elements


def format_hourly_summary(state, now):
    """Serialize an explicit card envelope for the unchanged persistent outbox."""
    state = _map(state)
    point = datetime.fromtimestamp(now, timezone.utc)
    utc_date = point.date().isoformat()
    accounts, pairs = _rows(state.get("accounts")), _rows(state.get("pairs"))
    account_by_id = {a.get("id"): a for a in accounts}
    snapshots = {aid: _map(a.get("snapshot")) for aid, a in account_by_id.items()}
    bound = {p.get(key) for p in pairs for key in ("long_account_id", "short_account_id")}
    for pair in pairs:
        for side in ("long", "short"):
            aid = pair.get(side + "_account_id")
            snapshots[aid] = _latest_snapshot({"snapshot": snapshots.get(aid)}, _map(_map(pair.get("state")).get("snapshots")).get(side))
    live = [snapshots[a["id"]] for a in accounts if a.get("mode") == "live" and "id" in a]
    pnl_values = [_number(s.get("unrealized")) for s in live]
    pnl = sum(pnl_values, Decimal(0)) if pnl_values and all(v is not None for v in pnl_values) and not (bound - account_by_id.keys()) else None
    elements = _metric("实盘合计浮盈亏 · USD1", _amount(pnl), "heading-1", "red" if pnl is not None and pnl < 0 else "green" if pnl is not None and pnl > 0 else "default")
    if live and any(not _fresh(s.get("timestamp"), now, 8) for s in live):
        elements.append(_label("浮盈亏含未刷新快照", "notation", "orange"))
    if not state.get("ready"):
        elements.append(_label("服务：未就绪", color="orange"))
    if state.get("demo"):
        elements.append(_label("演示环境 · 模拟数据", color="orange"))
    for error in (state.get("error"), _map(state.get("notification")).get("error")):
        if error:
            elements.append(_label("异常：" + _text(error), color="orange"))
    markets = _map(state.get("markets"))
    blocks = [_pair_block(p, account_by_id, markets, now, utc_date) for p in pairs]
    blocks += [_account_block(a, now, utc_date) for a in accounts if a.get("id") not in bound]
    blocks.sort(key=lambda block: not block[0])
    budget, relay = _map(state.get("request_budget")), _map(state.get("capacity_relay"))
    footer = ["WS " + ("已连接" if relay.get("connected") else "未连接" if relay else "未配置"),
              f"API {_amount(budget.get('used'), 0)} / {_amount(budget.get('limit'), 0)}" if budget else "API 暂无采样"]
    interval = _number(_map(_map(state.get("notification")).get("hourly_summary")).get("interval_seconds", 3600))
    tail = [{"tag": "hr"}, _label(" · ".join(footer), "notation"),
            _label(f"金额 USD1 · 交易量 UTC 日 {utc_date} · 每 {_amount(interval / 60, 0) if interval is not None else '待同步'} 分钟", "notation", "grey")]
    if relay.get("last_error"):
        tail.insert(1, _label("WS：" + _text(relay["last_error"]), color="orange"))
    retry = _number(budget.get("retry_after"))
    if retry is not None and retry > 0:
        tail.insert(1, _label(f"API 预算 / 冷却等待 {_amount(retry, 0)} 秒", color="orange"))
    card = {"schema": "2.0", "config": {"summary": {"content": "ASTER 定时运行摘要"}},
            "header": {"template": "turquoise", "title": {"tag": "plain_text", "content": "ASTER 运行摘要"},
                       "subtitle": {"tag": "plain_text", "content": point.astimezone(DISPLAY_ZONE).strftime("%m-%d %H:%M:%S UTC+8")}},
            "body": {"direction": "vertical", "padding": "16px", "vertical_spacing": "12px", "elements": elements}}
    omitted = 0
    for _, block in blocks:
        candidate = elements + [{"tag": "hr"}] + block
        card["body"]["elements"] = candidate + tail
        if len(json.dumps(card).encode("utf-8")) <= MAX_MESSAGE_BYTES - 600:
            elements = candidate
        else:
            omitted += 1
    if omitted:
        elements.append(_label(f"另有 {omitted} 个账户或配对组未展开，请在工作台查看；异常优先。", "notation"))
    if not blocks:
        elements.append(_label("尚未配置账户或配对组"))
    card["body"]["elements"] = elements + tail
    return FEISHU_CARD_PREFIX + json.dumps(card, ensure_ascii=False, separators=(",", ":"))
