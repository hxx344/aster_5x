"""Bounded Feishu text from published state only; no exchange or database I/O."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from .report_cache import REPORT_MAX_AGE


MAX_MESSAGE_BYTES = 12000
DISPLAY_ZONE = timezone(timedelta(hours=8))
PHASES = {
    "starting": "启动中", "waiting": "等待条件", "paused": "已暂停",
    "attention": "需要处理", "error": "异常", "reconciling": "核对中",
    "waiting_open": "等待开仓", "waiting_close": "等待平仓", "holding": "持仓中",
    "daily_limit": "当日目标已达", "complete": "已完成", "disabled": "未启用",
    "unknown": "结果未知", "accepted": "已受理待核对", "acknowledged": "已确认待刷新",
    "blocked": "已阻止", "cooldown": "冷却中", "confirmed": "已核实",
    "paper_confirmed": "模拟已核实", "rejected": "已拒绝",
}


def _map(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _text(value, fallback="待同步", limit=120):
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


def _progress(value, target, known):
    used, limit = _number(value), _number(target)
    goal = "未设置" if limit is None else "未设上限" if limit == 0 else f"{_amount(limit)} USD1"
    if not known or used is None or used < 0:
        recorded = f"（已记录 {_amount(used)} USD1）" if used is not None and used >= 0 else ""
        return f"待同步/待核实{recorded}；目标 {goal}"
    result = f"{_amount(used)} USD1 / {goal}"
    if limit is not None and limit > 0:
        result += f"（{used / limit * 100:.1f}%{'，已达标' if used >= limit else ''}）"
    return result


def _snapshot(snapshot, now):
    snapshot = _map(snapshot)
    if not snapshot:
        return "账户快照缺失，持仓与余额待同步"
    stamp = _number(snapshot.get("timestamp"))
    age = Decimal(str(now)) - stamp if stamp is not None else None
    freshness = ("时间无效，待同步" if age is None or age < 0 else
                 f"{age:.0f} 秒前" + ("，已超过 8 秒交易新鲜度" if age >= 8 else ""))
    ratio = _number(snapshot.get("margin_ratio"))
    lines = [f"快照 {freshness}；权益 {_amount(snapshot.get('equity'))}，可用 {_amount(snapshot.get('available'))} USD1",
             f"占用保证金 {_amount(snapshot.get('occupied_margin'))} USD1；保证金率 {f'{ratio * 100:.1f}%' if ratio is not None else '待同步'}；浮盈亏 {_amount(snapshot.get('unrealized'))} USD1"]
    positions = _rows(snapshot.get("positions"))
    held = [p for p in positions if _number(p.get("qty")) != 0]
    if not isinstance(snapshot.get("positions"), list):
        lines.append("持仓待同步")
    elif not held:
        lines.append("快照记录空仓")
    else:
        names = [f"{_text(p.get('symbol'), limit=24)} {'多' if p.get('side') == 'LONG' else '空' if p.get('side') == 'SHORT' else '方向待核实'} {_amount(p.get('qty'), 6)}（名义 {_amount(p.get('notional'))} USD1）" for p in held[:3]]
        lines.append("持仓：" + "；".join(names) + (f"；另 {len(held) - 3} 项" if len(held) > 3 else ""))
    return "\n".join(lines)


def _account_block(account, now, utc_date):
    cycle = _map(account.get("cycle_state"))
    mode = "实盘" if account.get("mode") == "live" else "模拟"
    lines = [f"账户 {_text(account.get('name'))}（{mode}，{'启用' if account.get('enabled') else '暂停'}）：{_phase(account.get('status'))}"]
    if account.get("reason"):
        lines.append(_text(account["reason"]))
    lines.append(_snapshot(account.get("snapshot"), now))
    if _map(account.get("cycle")).get("enabled"):
        daily, report = _map(cycle.get("daily_volume")), _map(cycle.get("report_status"))
        known = (report.get("status") == "ready" and _fresh(report.get("as_of"), now, REPORT_MAX_AGE)
                 and daily.get("utc_date") == utc_date and not daily.get("sync_pending") and not daily.get("error"))
        lines.append(f"UTC 日循环成交：{_progress(daily.get('volume'), _map(account.get('cycle')).get('daily_volume_limit'), known)}")
    urgent = account.get("status") in {"attention", "error", "reconciling"} or bool(account.get("pause_reason"))
    return urgent, "\n".join(lines)


def _pair_block(pair, accounts, now, utc_date):
    state, config = _map(pair.get("state")), _map(pair.get("cycle"))
    margin = _map(state.get("margin"))
    phase = state.get("phase")
    lines = [f"配对组 {_text(pair.get('name'))} · {_text(pair.get('symbol'))}：{'启用' if pair.get('enabled') else '暂停'} / {_phase(phase)}"]
    if pair.get("pause_reason") or state.get("reason"):
        lines.append(_text(pair.get("pause_reason") or state.get("reason")))
    if state.get("pending"):
        lines.append("订单/执行批次尚未完成核对")
    daily = _map(_map(state.get("daily_volume")).get(utc_date))
    known = not state.get("volume_unknown") and not state.get("pending") and _fresh(state.get("updated_at"), now, 120)
    for side, label in (("long", "A 多"), ("short", "B 空")):
        account = accounts.get(pair.get(side + "_account_id"), {})
        snapshot = _map(_map(state.get("snapshots")).get(side))
        published = _map(account.get("snapshot"))
        if (_number(published.get("timestamp")) or 0) > (_number(snapshot.get("timestamp")) or 0):
            snapshot = published
        mode = "实盘" if account.get("mode") == "live" else "模拟" if account.get("mode") == "paper" else "模式待同步"
        lines.append(f"{label} · {_text(account.get('name'), _text(pair.get(side + '_account_id')))}（{mode}）")
        if config.get("enabled"):
            lines.append("UTC 日循环成交：" + _progress(daily.get(side), config.get("daily_volume_limit"), known))
        lines.append(_snapshot(snapshot, now))
    lines.append(f"保证金划转：{_phase(margin.get('status'))}；{_text(margin.get('reason'))}")
    transfer = _map(margin.get("pending"))
    if transfer:
        source = {"long": "A 多", "short": "B 空"}.get(transfer.get("source"), "待核实")
        destination = {"long": "A 多", "short": "B 空"}.get(transfer.get("destination"), "待核实")
        lines.append(f"待核对划转 {source} → {destination}：{_amount(transfer.get('amount'))} USD1")
    urgent = (phase in {"attention", "error", "reconciling"} or bool(pair.get("pause_reason"))
              or bool(state.get("pending")) or bool(state.get("volume_unknown")) or bool(transfer)
              or margin.get("status") in {"blocked", "unknown", "rejected"})
    return urgent, "\n".join(lines)


def format_hourly_summary(state, now):
    """Format whitelisted fields; never infer missing amounts as zero."""
    state = _map(state)
    point = datetime.fromtimestamp(now, timezone.utc)
    utc_date = point.date().isoformat()
    accounts, pairs = _rows(state.get("accounts")), _rows(state.get("pairs"))
    live = sum(a.get("mode") == "live" for a in accounts)
    lines = ["ASTER 每小时运行摘要", point.astimezone(DISPLAY_ZONE).strftime("%Y-%m-%d %H:%M:%S UTC+8"),
             f"成交口径：UTC 日 {utc_date}；金额单位 USD1",
             f"服务：{'就绪' if state.get('ready') else '未就绪'}；实盘 {live} 个账户，模拟 {len(accounts) - live} 个；启用配对组 {sum(bool(p.get('enabled')) for p in pairs)}/{len(pairs)}"]
    if state.get("demo"):
        lines.append("演示环境，所有数据仅为模拟")
    for error in (state.get("error"), _map(state.get("notification")).get("error")):
        if error:
            lines.append("异常：" + _text(error))
    budget = _map(state.get("request_budget"))
    if budget:
        lines.append(f"API 当前窗口：估算 {_amount(budget.get('used'), 0)}/{_amount(budget.get('limit'), 0)}；本进程 {_amount(budget.get('local_used'), 0)}；Aster 同 IP 回报 {_amount(budget.get('aster_ip_used'), 0)}")
        lines.append(f"执行可用权重 {_amount(budget.get('ordinary_remaining'), 0)}；重置约 {_amount(budget.get('reset_after'), 0)} 秒；预算/冷却等待 {_amount(budget.get('retry_after'), 0)} 秒")
    else:
        lines.append("API 预算：暂无采样")
    relay = _map(state.get("capacity_relay"))
    if relay:
        lines.append(f"副服务器 WS：{'已连接' if relay.get('connected') else '未连接'}；缓存 {_amount(relay.get('cached_samples'), 0)} 项（连接状态不代表样本新鲜）")
        if relay.get("last_error"):
            lines.append("副服务器异常：" + _text(relay["last_error"]))
    account_by_id = {a.get("id"): a for a in accounts}
    bound = {p.get(key) for p in pairs for key in ("long_account_id", "short_account_id")}
    blocks = [_pair_block(p, account_by_id, now, utc_date) for p in pairs]
    blocks += [_account_block(a, now, utc_date) for a in accounts if a.get("id") not in bound]
    blocks.sort(key=lambda block: not block[0])
    text, omitted = "\n".join(lines), 0
    for _, block in blocks:
        if len((text + "\n\n" + block).encode("utf-8")) <= MAX_MESSAGE_BYTES - 250:
            text += "\n\n" + block
        else:
            omitted += 1
    if omitted:
        text += f"\n\n另有 {omitted} 个账户或配对组未展开，请在工作台查看；异常优先展示。"
    if not blocks:
        text += "\n\n尚未配置账户或配对组。"
    return text + "\n\n仅汇总已有记录；本次摘要未额外查询交易所。"
