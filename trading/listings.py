"""Public USD1 listing discovery, independent of the executable strategy universe."""
from copy import deepcopy
from datetime import datetime, timezone
import re
import time

import monitor
from .models import TradingError
from . import monitoring

STATE_KEY = "usd1_listings"
POLL_SECONDS = 60
STALE_SECONDS = 180


def parse_symbols(payload):
    rows = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise TradingError("交易对列表为空或格式无效")
    result, seen = {}, set()
    for row in rows:
        if not isinstance(row, dict):
            raise TradingError("交易对列表格式无效")
        if not isinstance(row.get("quoteAsset"), str) or not row["quoteAsset"]:
            raise TradingError("交易对列表缺少必要字段")
        if row["quoteAsset"] != "USD1":
            continue
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not re.fullmatch(r"[\w.-]{1,80}", symbol) or symbol in seen:
            raise TradingError("交易对代码无效或重复")
        seen.add(symbol)
        # The live catalog includes unrelated pre-listings with an empty
        # contractType. They must not disable the complete USD1 universe.
        if row.get("status") == "PENDING_TRADING" and row.get("contractType") == "":
            continue
        if not all(isinstance(row.get(key), str) and row[key] for key in ("status", "contractType")):
            raise TradingError("USD1 交易对缺少必要字段")
        if row["contractType"] != "PERPETUAL":
            continue
        stamp = row.get("onboardDate")
        onboard_at = stamp / 1000 if type(stamp) in (int, float) and 0 < stamp < 253402300800000 else None
        result[symbol] = {"symbol": symbol, "status": row["status"], "onboard_at": onboard_at}
    # Do not turn an upstream empty/partial universe into a new baseline.
    if not result:
        raise TradingError("尚未取得有效的 USD1 永续交易对列表")
    return result


def maximum_leverage(payload, symbol):
    rows = monitor.unwrap(payload).get("brackets")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise monitor.MonitorError("Risk brackets missing")
    matches = [row for row in rows if row.get("symbol") == symbol]
    if len(matches) != 1 or not isinstance(matches[0].get("riskBrackets"), list) or not matches[0]["riskBrackets"]:
        raise monitor.MonitorError("Risk brackets missing or ambiguous")
    maxima = []
    for tier in matches[0]["riskBrackets"]:
        if not isinstance(tier, dict):
            raise monitor.MonitorError("Invalid risk bracket tier")
        low, high = (monitor.number(tier.get(key)) for key in ("minOpenPosLeverage", "maxOpenPosLeverage"))
        if low < 1 or high < low or high > 10000 or low != int(low) or high != int(high):
            raise monitor.MonitorError("Invalid leverage range")
        maxima.append(int(high))
    leverage = max(maxima)
    return leverage, monitor.extract_bracket_cap(payload, symbol, leverage)


def notification_text(row, *, supplement=False):
    def stamp(value):
        return datetime.fromtimestamp(value, timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC") if value else "未知"
    def amount(key):
        return f"{row[key]} USD1" if row.get(key) is not None else "暂不可用"
    leverage = f"{row['max_leverage']}x" if row.get("max_leverage") is not None else "暂不可用"
    return "\n".join([
        "ASTER USD1 上新" + (" · 额度补全" if supplement else ""),
        f"交易对：{row['symbol']}", f"最大杠杆：{leverage}",
        f"该杠杆公开可用额度：{amount('capacity')}",
        f"公开剩余额度：{amount('remaining')}", f"该杠杆档位上限：{amount('bracket_cap')}",
        f"发现时间：{stamp(row['detected_at'])}", f"额度采样时间：{stamp(row.get('checked_at'))}",
        "公开可用额度取公开剩余额度与该杠杆档位上限的较小值，非账户可开额度。",
        "以上为采样时快照；缺失数据自动重试，补齐后另行通知。" if row.get("error") else "以上为采样时快照，实际额度随市场变化。",
    ])


class ListingMonitor:
    def __init__(self, store, market, shutdown):
        self.store, self.market, self.shutdown = store, market, shutdown
        self.catalog_due = 0
        self.detail_due = {}

    def poll(self):
        if self.shutdown.is_set():
            return POLL_SECONDS
        state = deepcopy(self.store.get(STATE_KEY) or {"initialized": False, "checked_at": None, "rows": {}})
        config = self.store.monitoring_settings()
        if not config["monitoring_enabled"]:
            return 5
        now = time.time()
        if config["discovery_enabled"] and time.monotonic() >= self.catalog_due:
            try:
                symbols = parse_symbols(self.market.listing_symbols())
                for symbol, current in symbols.items():
                    row = state["rows"].setdefault(symbol, {"symbol": symbol, "first_seen_at": now, "seen_trading": False})
                    if current["status"] == "TRADING" and not row["seen_trading"]:
                        is_new = state["initialized"] and not state.get("rebaseline", False)
                        row.update(seen_trading=True, is_new=is_new, detected_at=now,
                                   notification_phase="pending" if is_new else "baseline")
                    row.update(current)
                for symbol, row in state["rows"].items():
                    if symbol not in symbols:
                        row["status"] = "MISSING"
                state.update(initialized=True, checked_at=now, error=None, rebaseline=False)
            except Exception:
                state["error"] = "上新检查失败，保留上次结果并自动重试"
            self.store.save_listing_state(state, policy_revision=config["revision"])
            self.catalog_due = time.monotonic() + POLL_SECONDS
        if self.shutdown.is_set():
            return POLL_SECONDS
        eligible = [row for row in state["rows"].values() if row["status"] == "TRADING"
                    and monitoring.monitored(config, row["symbol"])
                    and time.monotonic() >= self.detail_due.get(row["symbol"], 0)]
        if not eligible:
            return 5
        # One symbol per job keeps requests spread out; a failed symbol cannot
        # starve newly listed or existing markets. New discoveries go first.
        row = min(eligible, key=lambda row: (row.get("notification_phase") != "pending",
                                            self.detail_due.get(row["symbol"], 0), row["symbol"]))
        symbol, alerts = row["symbol"], []
        config = self.store.monitoring_settings()
        if not monitoring.monitored(config, symbol):
            return 1
        try:
            detail = self.market.listing_detail(symbol)
            row.update(detail)
        except Exception:
            row["error"] = "公开杠杆或额度暂不可用，等待重试"
        # Failure retains the old sample and its timestamp. Only a successful
        # exact-tier read can complete a previously incomplete notification.
        phase = row.get("notification_phase")
        if phase == "pending" or phase == "incomplete" and not row.get("error"):
            supplement = phase == "incomplete"
            alerts.append((f"usd1-listing:{symbol}" + (":detail" if supplement else ""),
                           notification_text(row, supplement=supplement)))
            row["notification_phase"] = "incomplete" if row.get("error") else "queued"
        self.store.save_listing_state(state, alerts, policy_revision=config["revision"])
        self.detail_due[symbol] = time.monotonic() + POLL_SECONDS
        return 1
