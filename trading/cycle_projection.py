"""Project durable cycle progress plus its small, current diagnostic overlay."""
from .models import dec


def cycle_overlay(saved, updates):
    # Progress/config/position ownership stay in the ledger. Only identity and
    # phase metadata qualify this transient reason/diagnostic for later display.
    return {**{key: saved[key] for key in ("run_id", "updated_at", "phase", "reason") if key in saved},
            "diagnostic": None, **updates}


def project_cycle_state(account, saved, current_cycle, pending, daily, rolling, *, background_reports):
    if current_cycle.get("run_id") != saved.get("run_id"):
        current_cycle = {}
    cycle = {**saved, **current_cycle}
    # Durable phase/ownership must win after an executor completes.
    if saved.get("updated_at", 0) > current_cycle.get("updated_at", 0):
        cycle.update(saved, diagnostic=None)
    active_cycle = pending and pending["kind"] in ("cycle", "cycle_leverage")
    if active_cycle:
        cycle.update(phase="attention" if pending["status"] == "attention" else "reconciling",
                     reason=pending.get("last_error") or account["reason"],
                     active_batch={"stage": pending.get("phase", "leverage")})
    elif not account.get("cycle", {}).get("enabled"):
        cycle.update(phase="disabled", reason="多空循环未启用", active_batch=None)
    elif not account["enabled"]:
        cycle.update(phase="attention" if account.get("pause_reason") else "paused",
                     reason=account.get("pause_reason") or "循环已暂停，已有仓位和计时保留", active_batch=None)
    else:
        cycle.setdefault("phase", "waiting_open")
        cycle.setdefault("reason", "等待多空循环检查")
        # Background statistics describe their own as-of time; they
        # cannot overwrite the current executor phase or diagnostics.
        if cycle.get("phase") == "rolling_limit":
            cycle.update(phase="waiting_open", reason="滚动成交量限制已取消，等待重新核对开仓条件", diagnostic=None)
        if not background_reports and not cycle.get("opened_at") and daily["reached"]:
            cycle.update(phase="daily_limit", reason="已达到 UTC 每日成交量上限，待日额度满足后自动恢复")
        elif not background_reports and cycle.get("phase") == "daily_limit":
            previous_remaining = cycle.get("quota_remaining")
            if (daily["remaining"] is None or cycle.get("quota_utc_date") != daily["utc_date"] or
                    previous_remaining is not None and dec(daily["remaining"]) > dec(previous_remaining)):
                cycle.update(phase="waiting_open", reason="当日成交额度已恢复，等待重新核对开仓条件", diagnostic=None)
        cycle["active_batch"] = None
    if active_cycle or cycle.get("phase") not in ("waiting_open", "waiting_close", "daily_limit"):
        cycle["diagnostic"] = None
    return cycle
