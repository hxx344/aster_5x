"""Independent account workers and shared market polling for the Linux service."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import os
import re
import threading
import time

import monitor
from .exchange import BudgetWait, ExchangeError, LiveBroker, MarketData, RequestNotSent, credentials_for
from .execution import Executor
from .lock import ProcessLock
from .models import AccountModeError, Book, MIN_BATCH_NOTIONAL, MIN_OPEN_LEVERAGE, SYMBOLS, TIERS, TradingError, dec, leverage_candidates, minimum_open_leverage, next_leverage, opening_margin_limit, plan_pair, positive, wire
from .paper import DemoMarket, PaperBroker
from .store import dumps

LOG = logging.getLogger("aster.trading")
MAX_ACCOUNTS = 8
ACCOUNT_LIST_INTERVAL = 1
CAPACITY_POLL_INTERVAL = 2
CAPACITY_MONITOR_RESERVE = len(SYMBOLS) * 2 * 60 // CAPACITY_POLL_INTERVAL
PUBLIC_POLL_ALLOWANCE = CAPACITY_MONITOR_RESERVE + 120  # Includes REST quote fallback.
PRIORITY_TIERS = (10, 20)
DEFAULT_POLICY = {"symbols": list(SYMBOLS), "threshold": "10000", "order_notional": "1000", "margin_limit": "0.5",
                  "spread_limit": "0.0005", "min_open_leverage": MIN_OPEN_LEVERAGE}
EDITABLE_POLICY_FIELDS = {"threshold", "order_notional", "margin_limit", "min_open_leverage"}


@dataclass
class MarketCandidate:
    symbol: str
    leverage: int
    book: Book
    target: int | None = None

    @property
    def opening_leverage(self):
        return self.leverage if self.target is None else self.target


def validate_account(account):
    if not re.fullmatch(r"[a-z0-9_-]{1,32}", account.get("id", "")):
        raise TradingError("账户标识仅支持小写字母、数字、下划线和短横线")
    if not isinstance(account.get("name"), str) or not 1 <= len(account["name"].strip()) <= 50:
        raise TradingError("账户名称需为 1 至 50 个字符")
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,40}", account.get("env_prefix", "")):
        raise TradingError("环境变量前缀格式无效")
    if account.get("mode") not in ("paper", "live") or not isinstance(account.get("enabled"), bool):
        raise TradingError("账户运行模式无效")
    policy = account.get("policy", {})
    if not isinstance(policy, dict):
        raise TradingError("策略配置字段不完整")
    policy = {"min_open_leverage": MIN_OPEN_LEVERAGE, **policy}
    if set(policy) != set(DEFAULT_POLICY):
        raise TradingError("策略配置字段不完整")
    if not isinstance(policy["symbols"], list) or not policy["symbols"] or any(s not in SYMBOLS for s in policy["symbols"]) or len(set(policy["symbols"])) != len(policy["symbols"]):
        raise TradingError("交易市场配置无效")
    if positive(policy["threshold"], True) > 1000000000:
        raise TradingError("额度阈值超出配置范围")
    if not MIN_BATCH_NOTIONAL <= positive(policy["order_notional"]) <= 1000000:
        raise TradingError("单批每边上限必须为 500 至 1000000 USD1")
    if not 0 < positive(policy["margin_limit"]) <= 1:
        raise TradingError("风险上限必须大于 0 且不超过 100%")
    if not 0 < positive(policy["spread_limit"]) <= dec("0.0005"):
        raise TradingError("价差上限不得超过万 5")
    minimum_open_leverage(policy)
    account["policy"] = policy
    return account


def snapshot_json(snapshot, symbols):
    result = asdict(snapshot)
    result.pop("brackets", None)
    result.pop("fees", None)
    result["ratio"] = snapshot.ratio if snapshot.equity > 0 else None
    result["margin_ratio"] = snapshot.margin_ratio if snapshot.equity > 0 else None
    result["total_notional"] = snapshot.total_notional
    result["occupied_margin"] = snapshot.occupied_margin
    result["positions"] = [{**row, "notional": p.notional, "occupied_margin": p.occupied_margin}
                           for row, p in zip(result["positions"], snapshot.positions)]
    result["mode_checks"] = snapshot.mode_checks(symbols)
    return json.loads(dumps(result))


class Engine:
    def __init__(self, store, demo=False, market=None):
        self.store, self.demo = store, demo
        self.market = market or (DemoMarket() if demo else MarketData())
        self.shutdown = threading.Event()
        self.scheduler_event = threading.Event()
        self.thread = None
        self.process_lock = ProcessLock(store.path.with_suffix(".lock"))
        self.lock = threading.RLock()
        self.lifecycle_lock = threading.Lock()
        self.registration_lock = threading.Lock()
        self.accounts_generation = 0
        self.account_locks = {}
        self.budget_wait_events = {}
        self.brokers, self.signers, self.users = {}, {}, {}
        self.markets, self.views, self.rotation = {}, {}, {}
        self.wake_accounts, self.urgent_accounts = set(), set()
        self.priority_accounts, self.priority_levels = {}, {}
        self.priority_followups, self.active_priority_accounts = set(), set()
        self.active_priority_signals = {}
        self.account_backoff = {}
        self.ready = False
        self.error = "正在连接行情服务"
        self.notification_error = None
        self.capacity_notification_errors = {}
        if demo and not store.accounts():
            account = {"id": "demo", "name": "示例子账户", "mode": "paper", "env_prefix": "ASTER_DEMO", "enabled": False, "policy": {**DEFAULT_POLICY}}
            store.save_account(account)
            self.brokers["demo"] = PaperBroker("demo", self.market, store, seed=True)

    def account_lock(self, account_id):
        with self.lock:
            return self.account_locks.setdefault(account_id, threading.RLock())

    def view(self, account_id, **updates):
        with self.lock:
            current = self.views.setdefault(account_id, {"strategies": {}})
            current.update(updates)

    def strategy(self, account_id, symbol, reason, phase="waiting", **fields):
        with self.lock:
            current = self.views.setdefault(account_id, {"strategies": {}})
            current["strategies"][symbol] = {"reason": reason, "phase": phase, **fields}

    def broker(self, account):
        aid = account["id"]
        if aid not in self.brokers:
            if account["mode"] == "paper":
                self.brokers[aid] = PaperBroker(aid, self.market, self.store)
            else:
                if self.demo:
                    raise TradingError("模拟环境不能添加或连接实盘账户")
                creds = credentials_for(account["env_prefix"])
                signer = creds["signer"].lower()
                user = creds["user"].lower()
                with self.lock:
                    if signer in self.signers and self.signers[signer] != aid:
                        raise TradingError("同一 API signer 不能由多个账户执行器共用")
                    if user in self.users and self.users[user] != aid:
                        raise TradingError("同一真实账户不能通过不同 API signer 重复接入")
                    broker = LiveBroker(creds, self.market)
                    self.signers[signer] = aid
                    self.users[user] = aid
                    self.brokers[aid] = broker
        return self.brokers[aid]

    def live_allowed(self, account):
        return account["mode"] == "paper" or (not self.demo and os.environ.get("ASTER_ALLOW_LIVE") == "1")

    def scheduling(self, accounts):
        """Spread ordinary private reads across the shared IP budget."""
        live = [a for a in accounts if a["mode"] == "live"]
        budget = self.market.api.budget.snapshot() if isinstance(self.market, MarketData) else {"ordinary_limit": 1500}
        # Reserve the capacity feed and ordinary quote fallback. Use cold
        # round costs; no private position or balance cache crosses a mutation.
        capacity = max(1, budget.get("execution_limit", budget["ordinary_limit"]) - PUBLIC_POLL_ALLOWANCE)
        with self.lock:
            def cost(account):
                if not account["enabled"]:
                    return 90
                positions = self.views.get(account["id"], {}).get("snapshot", {}).get("positions", [])
                minimum = minimum_open_leverage(account["policy"])
                for symbol in account["policy"]["symbols"]:
                    pair = [p for p in positions if p["symbol"] == symbol]
                    if len(pair) != 2:
                        return 300  # Cold start may perform and confirm an upgrade.
                    leverage = pair[0]["leverage"]
                    caps = self.markets.get(symbol, {}).get("capacities", {})
                    threshold = dec(account["policy"]["threshold"])
                    if any(target > leverage and dec(caps.get(str(target), "0")) > threshold for target in leverage_candidates(minimum)):
                        return 300
                return 120
            costs = {a["id"]: cost(a) for a in live}
        period = 60 * sum(costs.values()) / capacity
        return {a["id"]: {
            "interval": max(10 if a["enabled"] else 60, period),
            "gap": 60 * costs[a["id"]] / capacity,
        } for a in live}

    @staticmethod
    def recovery_budget(broker):
        return getattr(broker, "reconciliation_budget", nullcontext)()

    def completed_snapshot(self, executor, broker, symbols):
        snapshot = executor.last_snapshot
        if snapshot is not None:
            try:
                snapshot.require_fresh()
                return snapshot
            except TradingError:
                pass
        with self.recovery_budget(broker):
            return broker.snapshot(symbols)

    def record_batch_outcome(self, account_id, executor):
        intent = executor.last_completed_intent
        if not intent or intent["kind"] != "pair":
            return
        key = f"order_cooldown:{account_id}:{intent['symbol']}"
        if intent["status"] == "complete":
            self.store.put(key, None)
            return
        if intent["status"] != "aborted":
            return
        previous = self.store.get(key) or {}
        count = min(5, previous.get("failures", 0) + 1)
        rejected = any(r.get("status") == "REJECTED" for r in intent["receipts"].values())
        delay = min(120, (30 if rejected else 15) * 2 ** (count - 1))
        self.store.put(key, {"failures": count, "until": time.time() + delay})
        self.store.event(account_id, "waiting", f"{intent['symbol']} 本批无新增仓位，新开单等待 {delay} 秒；继续核对已有订单")

    def poll_market(self, symbol):
        if self.shutdown.is_set():
            return CAPACITY_POLL_INTERVAL
        try:
            accounts = self.store.accounts()
            tiers = set(TIERS)
            # Network latency is part of the capacity snapshot's age.
            checked_at = time.time()
            capacities = {tier: value for tier, value in self.market.capacities(symbol, tiers).items() if tier in TIERS}
            row = {"status": "ok", "capacities": {str(k): wire(v) for k, v in capacities.items()},
                   "checked_at": checked_at}
            with self.lock:
                previous = self.markets.get(symbol, {})
                self.markets[symbol] = {**previous, **json.loads(dumps(row))}
                self.markets[symbol].pop("error", None)
            # Wake execution before notification storage or quote I/O can delay it.
            self.wake_capacity_accounts(symbol, capacities, checked_at, accounts)
            self.observe_capacity_alerts(symbol, capacities, checked_at)
            return CAPACITY_POLL_INTERVAL
        except (TradingError, KeyError, ValueError, TypeError) as exc:
            self.invalidate_capacity_alerts(symbol)
            with self.lock:
                previous = self.markets.get(symbol, {})
                self.markets[symbol] = {**previous, "status": "error", "error": str(exc) if isinstance(exc, TradingError) else "行情数据格式异常"}
            self.wake_capacity_accounts(symbol, {}, time.time(), [])
            return max(10, getattr(exc, "retry_after", 0))

    def poll_book(self, symbol):
        """Display quotes cannot hold up the shared capacity feed."""
        if self.shutdown.is_set():
            return 5
        try:
            book = self.market.book(symbol)
            book.require_fresh()
            with self.lock:
                row = self.markets.setdefault(symbol, {})
                row["book"] = json.loads(dumps({**asdict(book), "spread": book.spread}))
                row.pop("book_error", None)
            return 5
        except (TradingError, KeyError, ValueError, TypeError) as exc:
            with self.lock:
                row = self.markets.setdefault(symbol, {})
                row.pop("book", None)
                row["book_error"] = str(exc) if isinstance(exc, TradingError) else "盘口数据格式异常"
            return max(10, getattr(exc, "retry_after", 0))

    def wake_capacity_accounts(self, symbol, capacities, checked_at, accounts):
        """Merge availability edges without polling private accounts at the feed cadence."""
        fresh = -1 <= time.time() - checked_at <= 8
        by_id = {a["id"]: a for a in accounts}
        with self.lock:
            ids = set(by_id) | {aid for aid, market in self.priority_levels if market == symbol}
            for aid in ids:
                account = by_id.get(aid)
                levels = ()
                if fresh and account and account["enabled"] and self.live_allowed(account) and symbol in account["policy"]["symbols"]:
                    positions = [p for p in self.views.get(aid, {}).get("snapshot", {}).get("positions", []) if p["symbol"] == symbol]
                    current = max((p["leverage"] for p in positions), default=0)
                    threshold = dec(account["policy"]["threshold"])
                    required = max(threshold,
                                   sum((dec(p.get("qty", 0)).copy_abs() * dec(p.get("mark", 0)) for p in positions), dec(0)))
                    minimum = minimum_open_leverage(account["policy"])
                    levels = tuple(tier for tier in PRIORITY_TIERS
                                   if (tier > current and capacities.get(tier, dec(0)) > required)
                                   or (tier == current >= minimum and capacities.get(tier, dec(0)) > threshold))
                key = (aid, symbol)
                previous = self.priority_levels.get(key, ())
                if levels:
                    self.priority_levels[key] = levels
                    if set(levels) - set(previous):
                        self.priority_accounts.setdefault(aid, {})[symbol] = checked_at
                    elif symbol in self.priority_accounts.get(aid, {}):
                        # Refresh a queued opportunity while this account is busy/backing off.
                        self.priority_accounts[aid][symbol] = checked_at
                else:
                    self.priority_levels.pop(key, None)
                    self.priority_accounts.get(aid, {}).pop(symbol, None)
                if not self.priority_accounts.get(aid):
                    self.priority_accounts.pop(aid, None)
            if self.priority_accounts:
                self.scheduler_event.set()

    def priority_continuation(self, account_id):
        with self.lock:
            self.priority_followups.add(account_id)
        self.scheduler_event.set()

    @staticmethod
    def select_leverage(snapshot, symbol, capacities, book, policy, priority=False):
        options = {"threshold": policy["threshold"], "min_open_leverage": minimum_open_leverage(policy)}
        if priority:
            target = next_leverage(snapshot, symbol, {tier: value for tier, value in capacities.items() if tier in PRIORITY_TIERS}, book.mark, **options)
            if target is not None:
                return target
        return next_leverage(snapshot, symbol, capacities, book.mark, **options)

    def capacities(self, symbol):
        with self.lock:
            row = self.markets.get(symbol, {}).copy()
        if row.get("status") != "ok" or not -1 <= time.time() - row.get("checked_at", 0) <= 8:
            raise TradingError("市场额度快照过期或查询失败")
        capacities = {int(k): dec(v) for k, v in row["capacities"].items()}
        return capacities

    def check_post_fill_occupancy(self, account, snapshot):
        snapshot.require_modes(account["policy"]["symbols"])
        snapshot.require_fresh()
        limit = dec(account["policy"]["margin_limit"])
        batch = self.store.get("post_fill_check:" + account["id"])
        if not batch:
            # A partially filled pair has not written the completion marker yet.
            pending = self.store.intent(account["id"])
            batch = pending if pending and pending["kind"] == "pair" else None
        # Old boolean markers retain the base limit. An unrelated high-leverage
        # position must never grant a low-leverage batch extra opening room.
        if isinstance(batch, dict) and batch.get("leverage") in (10, 20):
            actual, _ = snapshot.pair(batch["symbol"])
            limit = opening_margin_limit(account["policy"], actual.leverage)
        ratio = snapshot.ratio if snapshot.equity > 0 else None
        over_limit = ratio is None or snapshot.margin_exceeds(limit)
        if over_limit:
            message = "成交后 USD1 账户总权益不足，已暂停新加仓" if ratio is None else "成交后保证金占用率超过上限，已暂停新加仓"
            self.store.pause_account(account, message)
            self.view(account["id"], status="attention", reason=message)
            self.store.event(account["id"], "error", message)
            self.store.finish_campaign(account, message, ratio)
        # Only clear the durable check after any required pause has been saved.
        self.store.put("post_fill_check:" + account["id"], None)
        return over_limit

    @staticmethod
    def rank_candidates(candidates):
        # Rank the usable opening tier first, including a pending upgrade target.
        # Stable sorting retains rotation only when both tier and spread tie.
        return sorted(candidates, key=lambda c: (-c.opening_leverage, c.book.spread_exact))

    def market_error(self, account_id, symbol, exc):
        reason = str(exc) if isinstance(exc, TradingError) else "交易数据格式异常"
        self.strategy(account_id, symbol, reason, "waiting")
        if isinstance(exc, (AccountModeError, RequestNotSent)) or self.store.intent(account_id):
            # No new symbol work until an existing intent is resolved.
            raise exc
        if isinstance(exc, ExchangeError) and exc.retry_after:
            raise exc
        return reason

    def tick_account(self, account_id):
        with self.account_lock(account_id):
            with self.lock:
                priority = account_id in self.active_priority_accounts
                priority_signals = self.active_priority_signals.pop(account_id, {})
            progressed, consumed_symbol, retry_priority = False, None, False
            if self.shutdown.is_set():
                return 5
            account = self.store.account(account_id)
            if not account:
                return 5
            try:
                broker = self.broker(account)
                pending = self.store.intent(account_id)
                recovery = bool(pending or self.store.get("post_fill_check:" + account_id))
                with self.recovery_budget(broker) if recovery else nullcontext():
                    if isinstance(broker, LiveBroker) and not recovery:
                        broker.api.budget.require_available(broker.snapshot_weight(account["policy"]["symbols"]))
                    snapshot = broker.snapshot(account["policy"]["symbols"])
                self.view(account_id, snapshot=snapshot_json(snapshot, account["policy"]["symbols"]), credential_ready=True)
                snapshot.require_modes(account["policy"]["symbols"])
                if self.shutdown.is_set():
                    return 5
                if self.store.get("post_fill_check:" + account_id) and self.check_post_fill_occupancy(account, snapshot):
                    return 5
                pause_reason = account.get("pause_reason") if not account["enabled"] else None
                self.view(account_id, status="running" if account["enabled"] else "attention" if pause_reason else "paused",
                          reason="策略运行中" if account["enabled"] else pause_reason or "策略已暂停")
                executor = Executor(self.store, broker, self.market)
                if pending:
                    if self.live_allowed(account):
                        reason = executor.reconcile(account, pending)
                        current = self.store.intent(account_id)
                        status = "attention" if current and current["status"] == "attention" else "reconciling"
                    else:
                        status, reason = "attention", "服务器未启用实盘执行，保留未完成批次等待核对"
                    self.view(account_id, status=status, reason=reason)
                    self.strategy(account_id, pending["symbol"], reason, status)
                    progressed = True
                    if pending["kind"] == "pair" and not self.store.intent(account_id):
                        consumed_symbol = pending["symbol"]
                        self.record_batch_outcome(account_id, executor)
                        after = self.completed_snapshot(executor, broker, account["policy"]["symbols"])
                        self.view(account_id, snapshot=snapshot_json(after, account["policy"]["symbols"]))
                        self.check_post_fill_occupancy(account, after)
                    elif (pending["kind"] == "leverage" and pending["target"] in PRIORITY_TIERS
                          and not self.store.intent(account_id) and account["enabled"] and self.live_allowed(account)
                          and self.store.get(f"open_after_leverage:{account_id}:{pending['symbol']}") is not None):
                        self.priority_continuation(account_id)
                    return 5
                if not account["enabled"] or self.shutdown.is_set():
                    for symbol in account["policy"]["symbols"]:
                        self.strategy(account_id, symbol, "策略已暂停", "paused")
                    if snapshot.equity > 0:
                        self.store.finish_campaign(account, "策略已暂停", snapshot.ratio)
                    return 5
                if not self.live_allowed(account):
                    raise TradingError("服务器尚未设置 ASTER_ALLOW_LIVE=1")
                policy = account["policy"]
                minimum = minimum_open_leverage(policy)
                markets = policy["symbols"]
                start = self.rotation.get(account_id, 0) % len(markets)
                ordered = markets[start:] + markets[:start]
                last_reason = "等待交易条件"
                candidates = []
                first_add_symbols = set()
                campaign_limit = dec(policy["margin_limit"])
                for symbol in ordered:
                    try:
                        long, short = snapshot.require_ready(symbol)
                        campaign_limit = max(campaign_limit, opening_margin_limit(policy, long.leverage))
                        if self.market.rules[symbol].margin_asset != "USD1":
                            raise TradingError("仅允许 USD1 保证金市场")
                        capacities = self.capacities(symbol)
                        book = self.market.book(symbol)
                        book.require_fresh()
                        if self.shutdown.is_set():
                            return 5
                        target = None
                        current_available = capacities.get(long.leverage, dec(0)) > dec(policy["threshold"])
                        first_add = long.leverage >= minimum and self.store.get(f"open_after_leverage:{account_id}:{symbol}") == long.leverage and current_available
                        if first_add and priority and long.leverage not in PRIORITY_TIERS:
                            high_target = self.select_leverage(snapshot, symbol, capacities, book, policy, True)
                            if high_target in PRIORITY_TIERS:
                                first_add = False
                        if not first_add:
                            target = self.select_leverage(snapshot, symbol, capacities, book, policy, priority)
                        if target is not None:
                            candidates.append(MarketCandidate(symbol, long.leverage, book, target))
                            continue
                        if long.leverage < minimum:
                            last_reason = f"当前 {long.leverage}x 低于 {minimum}x，禁止新增开仓；等待可用的更高杠杆档位"
                            self.strategy(account_id, symbol, last_reason)
                            continue
                        cooldown = self.store.get(f"order_cooldown:{account_id}:{symbol}") or {}
                        if cooldown.get("until", 0) > time.time():
                            last_reason = f"上批无新增仓位，新开单冷却中（剩余 {max(1, int(cooldown['until'] - time.time()))} 秒）"
                            self.strategy(account_id, symbol, last_reason)
                            continue
                        plan = plan_pair(snapshot, book, self.market.rules[symbol], capacities, policy)
                        if first_add and not plan.qty:
                            # A full current initial margin must not permanently block
                            # a further upgrade that could free balance for the first add.
                            self.store.put(f"open_after_leverage:{account_id}:{symbol}", None)
                        self.strategy(account_id, symbol, plan.reason, projected_ratio=wire(plan.projected_ratio) if plan.projected_ratio is not None else None)
                        last_reason = plan.reason
                        if plan.qty:
                            candidates.append(MarketCandidate(symbol, long.leverage, book))
                            if first_add and long.leverage in PRIORITY_TIERS:
                                first_add_symbols.add(symbol)
                    except (TradingError, KeyError, ValueError, TypeError) as exc:
                        last_reason = self.market_error(account_id, symbol, exc)
                if priority_signals:
                    # Unrelated filled markets must not repeatedly consume another
                    # market's queued opportunity. Keep confirmed first adds eligible.
                    preferred = [c for c in candidates if c.symbol in priority_signals or c.symbol in first_add_symbols]
                    if preferred:
                        candidates = preferred
                    else:
                        priority_signals = {}
                for candidate in self.rank_candidates(candidates):
                    if self.shutdown.is_set():
                        return 5
                    symbol, book, target = candidate.symbol, candidate.book, candidate.target
                    try:
                        # Comparing markets can take time. Reuse the sampled BBO
                        # only while fresh, and recheck the shared capacity cache.
                        snapshot.require_ready(symbol)
                        book.require_fresh()
                        capacities = self.capacities(symbol)
                        if target is not None:
                            if self.select_leverage(snapshot, symbol, capacities, book, policy, priority) != target:
                                last_reason = "可用杠杆档位已变化，等待下一轮比较"
                                self.strategy(account_id, symbol, last_reason)
                                continue
                            if isinstance(broker, LiveBroker):
                                broker.api.budget.require_available(broker.snapshot_weight(markets, fresh_modes=True) + 1)
                            def validate_upgrade(current):
                                latest_book = self.market.book(symbol)
                                latest_book.require_fresh()
                                latest = self.capacities(symbol)
                                if next_leverage(current, symbol, {target: latest.get(target, dec(0))}, latest_book.mark,
                                                 threshold=policy["threshold"], min_open_leverage=minimum) != target:
                                    raise TradingError("目标杠杆额度或账户条件已变化，等待新机会")
                            reason = executor.leverage(account, symbol, candidate.leverage, target, snapshot=snapshot,
                                                       before_submit=validate_upgrade)
                            progressed = True
                            self.strategy(account_id, symbol, reason, "leverage")
                            self.rotation[account_id] = (markets.index(symbol) + 1) % len(markets)
                            if target in PRIORITY_TIERS:
                                self.priority_continuation(account_id)
                            return 5
                        plan = plan_pair(snapshot, book, self.market.rules[symbol], capacities, policy)
                        if not plan.qty:
                            last_reason = plan.reason
                            self.strategy(account_id, symbol, last_reason)
                            continue
                        reason = executor.open_pair(account, snapshot, symbol, plan, book)
                        progressed, consumed_symbol = True, symbol
                        # A completed pair is followed by an actual account risk check.
                        self.record_batch_outcome(account_id, executor)
                        after = self.completed_snapshot(executor, broker, markets)
                        unresolved = self.store.intent(account_id)
                        phase = ("attention" if unresolved["status"] == "attention" else "reconciling") if unresolved else "filled"
                        self.view(account_id, snapshot=snapshot_json(after, markets), reason=reason, status=phase if unresolved else "running")
                        after.require_modes(markets)
                        self.strategy(account_id, symbol, reason, phase)
                        self.check_post_fill_occupancy(account, after)
                        self.rotation[account_id] = (markets.index(symbol) + 1) % len(markets)
                        return 5
                    except (TradingError, KeyError, ValueError, TypeError) as exc:
                        last_reason = self.market_error(account_id, symbol, exc)
                self.view(account_id, reason=last_reason)
                campaign = self.store.get("campaign:" + account_id)
                if campaign and (snapshot.margin_exceeds(campaign_limit, include_equal=True) or time.time() - campaign["last_fill_at"] >= 60):
                    self.store.finish_campaign(account, last_reason, snapshot.ratio)
                return 5
            except (TradingError, KeyError, ValueError, TypeError) as exc:
                message = str(exc) if isinstance(exc, TradingError) else "账户响应格式异常，已停止本轮操作"
                with self.lock:
                    old_reason = self.views.get(account_id, {}).get("reason")
                    log_wait = not isinstance(exc, BudgetWait) or time.monotonic() - self.budget_wait_events.get(account_id, -1e9) >= 60
                    if isinstance(exc, BudgetWait) and old_reason != message and log_wait:
                        self.budget_wait_events[account_id] = time.monotonic()
                if old_reason != message and log_wait:
                    self.store.event(account_id, "wait" if isinstance(exc, BudgetWait) else "error", message)
                status = "waiting" if isinstance(exc, BudgetWait) else "error"
                if isinstance(exc, AccountModeError):
                    self.store.pause_account(account, message)
                    pending = self.store.intent(account_id)
                    if pending:
                        pending.update(status="attention", last_error=message)
                        self.store.save_intent(pending)
                    for symbol in account["policy"]["symbols"]:
                        self.strategy(account_id, symbol, message, "attention")
                    status = "attention"
                self.view(account_id, status=status, reason=message, credential_ready=account_id in self.brokers)
                delay = max(10, getattr(exc, "retry_after", 0))
                with self.lock:
                    self.account_backoff[account_id] = time.monotonic() + delay
                if priority and isinstance(exc, ExchangeError):
                    retry_priority = True
                    self.priority_continuation(account_id)
                return delay
            finally:
                urgent = bool(self.store.intent(account_id) or self.store.get("post_fill_check:" + account_id))
                with self.lock:
                    self.active_priority_accounts.discard(account_id)
                    if progressed or retry_priority:
                        # Keep each opportunity through upgrade and confirmation;
                        # consume it after its first batch, retaining the other markets.
                        for symbol in priority_signals:
                            row = self.markets.get(symbol, {})
                            if (symbol != consumed_symbol and self.priority_levels.get((account_id, symbol))
                                and row.get("status") == "ok" and -1 <= time.time() - row.get("checked_at", 0) <= 8):
                                self.priority_accounts.setdefault(account_id, {}).setdefault(symbol, row["checked_at"])
                    if urgent:
                        self.urgent_accounts.add(account_id)
                    else:
                        self.urgent_accounts.discard(account_id)

    def add_account(self, data):
        account = validate_account({**data, "enabled": False, "policy": {**DEFAULT_POLICY}})
        if self.demo and account["mode"] != "paper":
            raise TradingError("模拟环境只接受模拟账户")
        with self.registration_lock:
            accounts = self.store.accounts()
            if len(accounts) >= MAX_ACCOUNTS:
                raise TradingError("单实例最多管理 8 个账户")
            if any(a["id"] == account["id"] or a["env_prefix"] == account["env_prefix"] for a in accounts):
                raise TradingError("账户标识或环境变量前缀已使用")
            self.store.save_account(account)
            with self.lock:
                self.accounts_generation += 1
        self.store.event(account["id"], "config", "账户已添加，默认暂停")
        return account

    def configure(self, account_id, changes):
        with self.account_lock(account_id):
            account = self.store.account(account_id)
            if not account:
                raise TradingError("账户不存在")
            if account["enabled"] or self.store.intent(account_id):
                raise TradingError("请先暂停策略并等待当前批次完成")
            if not isinstance(changes, dict) or not changes or set(changes) - EDITABLE_POLICY_FIELDS:
                raise TradingError("请选择有效的策略配置字段")
            if any(value is None or (key != "min_open_leverage" and not isinstance(value, str)) for key, value in changes.items()):
                raise TradingError("策略配置字段类型无效")
            account["policy"] = {**account["policy"], **changes}
            validate_account(account)
            if "min_open_leverage" in changes:
                account.pop("leverage_setting_required", None)
            self.store.save_account(account)
            with self.lock:
                self.accounts_generation += 1
            self.store.event(account_id, "config", "策略设置已更新")

    def enable(self, account_id, enabled):
        with self.account_lock(account_id):
            account = self.store.account(account_id)
            if not account:
                raise TradingError("账户不存在")
            view_updates = {}
            if enabled:
                if account.get("leverage_setting_required"):
                    raise TradingError("请先在 5x、10x、20x 中保存最低开仓杠杆设置")
                if not self.live_allowed(account):
                    raise TradingError("服务器尚未启用实盘执行（ASTER_ALLOW_LIVE=1）")
                if dec(account["policy"]["order_notional"]) < MIN_BATCH_NOTIONAL:
                    raise TradingError("单批每边上限低于固定最低批次金额 500 USD1，请先修改策略设置")
                if self.store.intent(account_id):
                    raise TradingError("请先核对未完成批次")
                snapshot = self.broker(account).snapshot(account["policy"]["symbols"], fresh_modes=True)
                for symbol in account["policy"]["symbols"]:
                    snapshot.require_ready(symbol)
                view_updates = {"snapshot": snapshot_json(snapshot, account["policy"]["symbols"]), "credential_ready": True,
                                "strategies": {symbol: {"phase": "waiting", "reason": "策略已启动，等待下一轮检查"}
                                               for symbol in account["policy"]["symbols"]}}
                # High existing occupancy blocks additions in plan_pair, while an
                # authorized increase in leverage can release margin before adding.
            account["enabled"] = enabled
            if enabled:
                account.pop("pause_reason", None)
            self.store.save_account(account)
            with self.lock:
                self.wake_accounts.add(account_id)
                self.accounts_generation += 1
            self.view(account_id, status="running" if enabled else "attention" if account.get("pause_reason") else "paused",
                      reason="策略运行中" if enabled else account.get("pause_reason") or "策略已暂停", **view_updates)
            self.store.event(account_id, "control", "策略已启动" if enabled else "策略已暂停；已提交批次继续核对")

    def retry(self, account_id):
        with self.account_lock(account_id):
            intent = self.store.intent(account_id)
            if not intent:
                return
            intent["status"] = "pending"
            if intent["kind"] == "pair":
                intent["repair_attempts"] = 0
            self.store.save_intent(intent)
            with self.lock:
                self.urgent_accounts.add(account_id)
                self.wake_accounts.add(account_id)
            self.store.event(account_id, "control", "重新核对未完成批次；不重复提交原开仓订单")

    def notification_config(self):
        webhook = os.environ.get("FEISHU_WEBHOOK_URL", "")
        if not webhook:
            return None
        base = json.loads((monitor.ROOT / "config.json").read_text(encoding="utf-8"))
        return monitor.validate_config({**base, "feishu_enabled": True, "feishu_webhook": webhook,
                                       "feishu_sign_secret": os.environ.get("FEISHU_SIGN_SECRET", "")})

    def capacity_alert_config(self, notification=None):
        if self.demo:
            return None
        enabled = os.environ.get("ASTER_CAPACITY_ALERT_ENABLED", "1").strip()
        if enabled not in ("0", "1"):
            raise TradingError("额度提醒开关必须为 0 或 1")
        if enabled == "0":
            return None
        config = notification if notification is not None else self.notification_config()
        if config is None:
            return None
        cooldown = positive(os.environ.get("ASTER_CAPACITY_ALERT_COOLDOWN_SECONDS", str(config["cooldown_seconds"])), True)
        if cooldown > 86400:
            raise TradingError("额度提醒冷却时间超出范围")
        accounts = sorted(self.store.accounts(), key=lambda account: account["id"])
        markets = {}
        for symbol in SYMBOLS:
            sources = []
            for account in accounts:
                if symbol not in account["policy"]["symbols"]:
                    continue
                threshold = positive(account["policy"]["threshold"], True)
                if threshold > 1000000000:
                    raise TradingError("网页额度阈值超出范围")
                sources.append((account["id"], account["name"], threshold))
            if not sources:
                continue
            # Bind queued messages to the saved web settings. Changing a setting
            # invalidates an old candidate, including during delivery retries.
            identity_sources = [(aid, name, threshold.as_integer_ratio()) for aid, name, threshold in sources]
            identity = hashlib.sha256(f"{config['webhook']}|{symbol}|{identity_sources!r}".encode()).hexdigest()
            markets[symbol] = {"threshold": min(source[2] for source in sources),
                               "identity": identity, "accounts": sources}
        return {"markets": markets, "cooldown": float(cooldown)}

    def invalidate_capacity_alerts(self, symbol, leverage=None):
        try:
            self.store.invalidate_capacity_alert(symbol, leverage)
            return True
        except Exception:
            # A notification failure must not take down price polling or trading.
            with self.lock:
                self.capacity_notification_errors[symbol] = "额度提醒状态保存失败，等待重试"
            return False

    def observe_capacity_alerts(self, symbol, capacities, checked_at):
        if self.demo or self.shutdown.is_set():
            return
        try:
            policy = self.capacity_alert_config()
            market_policy = policy["markets"].get(symbol) if policy else None
            if market_policy is None:
                if self.invalidate_capacity_alerts(symbol):
                    with self.lock:
                        self.capacity_notification_errors.pop(symbol, None)
                return
            for leverage in TIERS:
                if leverage not in capacities:
                    if not self.invalidate_capacity_alerts(symbol, leverage):
                        return
                    continue
                self.store.observe_capacity_alert(symbol, leverage, capacities[leverage],
                    threshold=market_policy["threshold"], cooldown=policy["cooldown"],
                    identity=market_policy["identity"], checked_at=checked_at,
                    account_labels=[f"{name}（{aid}，> {threshold:,.2f} USD1）"
                                    for aid, name, threshold in market_policy["accounts"]
                                    if dec(capacities[leverage]) > threshold])
            with self.lock:
                self.capacity_notification_errors.pop(symbol, None)
        except (TradingError, monitor.MonitorError, ValueError, TypeError, OSError):
            self.invalidate_capacity_alerts(symbol)
            with self.lock:
                self.capacity_notification_errors[symbol] = "额度提醒配置或数据无效，等待修正"
        except Exception:
            with self.lock:
                self.capacity_notification_errors[symbol] = "额度提醒状态保存失败，等待重试"

    def notify(self):
        if self.demo or self.shutdown.is_set():
            return 5
        try:
            config = self.notification_config()
            if not config:
                return 5
            try:
                capacity_policy = self.capacity_alert_config(config)
                with self.lock:
                    self.capacity_notification_errors.pop("config", None)
            except (TradingError, ValueError, TypeError):
                capacity_policy = None
                with self.lock:
                    self.capacity_notification_errors["config"] = "额度提醒配置无效；成交汇总继续发送"
            capacity_sent = 0
            for item in self.store.due_notifications():
                if self.shutdown.is_set():
                    break
                # Re-read after earlier sends: a capacity value may have changed
                # or expired while another webhook request was in flight.
                item = self.store.notification_for_delivery(item["id"])
                if item is None:
                    continue
                if item.get("capacity_key"):
                    # Settings may change while an earlier Feishu request is in flight.
                    try:
                        capacity_policy = self.capacity_alert_config(config)
                    except (TradingError, ValueError, TypeError):
                        capacity_policy = None
                        with self.lock:
                            self.capacity_notification_errors["config"] = "额度提醒配置无效；成交汇总继续发送"
                    symbol = item["capacity_key"].split(":")[1]
                    market_policy = capacity_policy["markets"].get(symbol) if capacity_policy else None
                    if market_policy is None or item.get("capacity_identity") != market_policy["identity"] or capacity_sent >= 2:
                        continue
                    capacity_sent += 1
                try:
                    monitor.send_feishu(config, item["message"])
                    self.store.notification_result(item, True)
                    self.notification_error = None
                except monitor.MonitorError:
                    self.store.notification_result(item, False)
                    self.notification_error = "飞书发送失败，等待重试"
        except monitor.MonitorError:
            self.notification_error = "飞书配置无效"
        return 5

    def state(self):
        # Database I/O must not hold the view lock shared by all account workers.
        saved_accounts = self.store.accounts()
        events = self.store.events()
        pending_notifications = self.store.pending_notifications()
        request_budget = self.market.api.budget.snapshot() if isinstance(self.market, MarketData) else None
        with self.lock:
            accounts = [{**a, "risk_limits": {"base": a["policy"]["margin_limit"],
                                            "high_leverage": wire(opening_margin_limit(a["policy"], 10))},
                         **self.views.get(a["id"], {
                "status": "attention" if a.get("pause_reason") else "starting",
                "reason": a.get("pause_reason") or "等待读取账户", "credential_ready": False, "strategies": {}})} for a in saved_accounts]
            return json.loads(dumps({"demo": self.demo, "ready": self.ready, "error": self.error,
                "accounts": accounts, "markets": self.markets, "events": events, "updated_at": time.time(), "request_budget": request_budget,
                "notification": {"configured": bool(os.environ.get("FEISHU_WEBHOOK_URL")), "pending": pending_notifications,
                                 "error": self.notification_error or next(iter(self.capacity_notification_errors.values()), None)}}))

    def run(self):
        pending, due = {}, {}
        account_ids, account_generation, accounts_due = [], -1, 0
        schedules, known_accounts, next_ordinary_start = {}, set(), 0
        # Capacity, quotes, accounts and the outbox each have worker capacity.
        try:
            if isinstance(self.market, MarketData) and not self.shutdown.is_set():
                self.market.api.budget.configure_capacity_reserve(CAPACITY_MONITOR_RESERVE)
                try:
                    self.market.start_stream()
                except Exception:
                    LOG.warning("Public quote stream unavailable; using REST quotes")
            with ThreadPoolExecutor(max_workers=MAX_ACCOUNTS + 2 * len(SYMBOLS) + 1, thread_name_prefix="aster") as pool:
                try:
                    while not self.shutdown.is_set():
                        self.scheduler_event.clear()
                        if not self.ready:
                            try:
                                self.market.load_rules()
                                if any(s not in self.market.rules for s in SYMBOLS):
                                    raise TradingError("缺少配置市场的交易规则")
                                self.ready, self.error = True, None
                            except (TradingError, KeyError, ValueError, TypeError) as exc:
                                self.error = str(exc) if isinstance(exc, TradingError) else "交易规则加载失败"
                                self.shutdown.wait(max(10, getattr(exc, "retry_after", 0)))
                                continue
                        for key, future in list(pending.items()):
                            if future.done():
                                try:
                                    delay = future.result()
                                except Exception:
                                    LOG.error("Worker failed; new work delayed (%s)", key)
                                    delay = 30
                                aid = key.removeprefix("account:")
                                with self.lock:
                                    urgent = aid in self.urgent_accounts
                                    self.active_priority_accounts.discard(aid)
                                    self.active_priority_signals.pop(aid, None)
                                if key.startswith("account:") and aid in schedules and not urgent:
                                    delay = max(delay, schedules[aid]["interval"])
                                due[key] = time.monotonic() + delay
                                del pending[key]
                        with self.lock:
                            generation = self.accounts_generation
                        if generation != account_generation or time.monotonic() >= accounts_due:
                            saved_accounts = self.store.accounts()
                            account_ids = [a["id"] for a in saved_accounts]
                            schedules = self.scheduling(saved_accounts)
                            for aid in set(account_ids) - known_accounts:
                                if self.store.intent(aid) or self.store.get("post_fill_check:" + aid):
                                    with self.lock:
                                        self.urgent_accounts.add(aid)
                            known_accounts = set(account_ids)
                            account_generation = generation
                            accounts_due = time.monotonic() + ACCOUNT_LIST_INTERVAL
                        with self.lock:
                            for aid in list(self.wake_accounts):
                                key = "account:" + aid
                                if key not in pending:
                                    due[key] = 0
                                    self.wake_accounts.discard(aid)
                            urgent_accounts = self.urgent_accounts.copy()
                            priority_accounts = self.priority_followups.copy()
                            for aid, signals in self.priority_accounts.items():
                                if aid not in urgent_accounts and any(-1 <= time.time() - stamp <= 8
                                       and self.markets.get(symbol, {}).get("status") == "ok"
                                       for symbol, stamp in signals.items()):
                                    priority_accounts.add(aid)
                        jobs = {"market:" + s: (self.poll_market, s) for s in SYMBOLS}
                        jobs.update({"book:" + s: (self.poll_book, s) for s in SYMBOLS})
                        jobs["notify"] = (self.notify,)
                        ordered_accounts = sorted(account_ids, key=lambda aid: (aid not in urgent_accounts, aid not in priority_accounts))
                        jobs.update({"account:" + aid: (self.tick_account, aid) for aid in ordered_accounts})
                        for key, (function, *args) in jobs.items():
                            is_account = key.startswith("account:")
                            aid = key.removeprefix("account:")
                            priority = is_account and aid in priority_accounts
                            if key not in pending and (priority or time.monotonic() >= due.get(key, 0)):
                                if is_account:
                                    with self.lock:
                                        if time.monotonic() < self.account_backoff.get(aid, 0):
                                            continue
                                ordinary = is_account and aid in schedules and aid not in urgent_accounts and not priority
                                if ordinary and time.monotonic() < next_ordinary_start:
                                    continue
                                if priority:
                                    with self.lock:
                                        self.active_priority_signals[aid] = self.priority_accounts.pop(aid, {})
                                        self.priority_followups.discard(aid)
                                        self.active_priority_accounts.add(aid)
                                pending[key] = pool.submit(function, *args)
                                if ordinary or priority and aid in schedules and aid not in urgent_accounts:
                                    next_ordinary_start = max(next_ordinary_start, time.monotonic() + schedules[aid]["gap"])
                        self.scheduler_event.wait(.1)
                finally:
                    # Fail health checks as soon as scheduling ends, before waiting
                    # for in-flight workers and closing their shared clients.
                    self.shutdown.set()
                    with self.lock:
                        self.ready, self.error = False, "交易调度已停止"
        except Exception:
            with self.lock:
                self.error = "交易调度异常停止，请检查服务后重启"
            LOG.error("Scheduler stopped unexpectedly")
        finally:
            self.shutdown.set()
            self.scheduler_event.set()
            with self.lock:
                self.ready = False
                brokers = list(self.brokers.items())
            # The pool context has joined all workers. One failing close must not
            # prevent the remaining clients from releasing their resources.
            for account_id, broker in brokers:
                try:
                    broker.close()
                except Exception:
                    LOG.error("Account client close failed (%s)", account_id)
            if isinstance(self.market, MarketData):
                try:
                    self.market.close_stream()
                except Exception:
                    LOG.error("Public quote stream close failed")
                try:
                    self.market.api.close()
                except Exception:
                    LOG.error("Market client close failed")

    def start(self):
        with self.lifecycle_lock:
            if self.thread is not None:
                raise TradingError("交易服务已经启动；请勿重复启动同一执行器")
            if self.shutdown.is_set():
                raise TradingError("交易服务正在停止或已停止，请重新创建执行器")
            self.process_lock.acquire()
            try:
                self.thread = threading.Thread(target=self.run, name="aster-engine", daemon=True)
                self.thread.start()
            except BaseException:
                self.thread = None
                self.process_lock.release()
                raise

    def stop(self):
        with self.lifecycle_lock:
            self.shutdown.set()
            self.scheduler_event.set()
            if self.thread:
                self.thread.join(timeout=90)
            if not self.thread or not self.thread.is_alive():
                self.process_lock.release()
