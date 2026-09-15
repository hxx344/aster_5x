"""Independent account workers and shared market polling for the Linux service."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from fractions import Fraction
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import uuid

import monitor
from .depth import DEPTH_POLL_INTERVAL, DEPTH_RESYNC_INTERVAL, DEPTH_WEIGHT
from .exchange import BudgetWait, ExchangeError, LiveBroker, MarketData, RequestNotSent, credentials_for
from .account_cache import HotAccountUnavailable
from .execution import Executor
from .cycle import DEFAULT_CYCLE, CyclePositionError, DailyVolumeLimitError, RollingVolumeLimitError, cycle_symbols, cycle_config, plan_cycle, validate_cycle, validate_cycle_positions
from .cycle_execution import CycleExecutor
from .cycle_cost import calculate_cycle_costs
from .cycle_diagnostics import CycleConditionError, diagnostic_error, diagnostic_number
from .cycle_guard import ordinary_add_blocks, ordinary_add_symbols, ordinary_selection
from .cycle_quality import clock_tick, elapsed as observed_elapsed
from .cycle_signal import cycle_signal_quote
from .models import cycle_margin_limit
from .lock import ProcessLock
from .migration import DEFAULT_MIGRATION, migration_symbols, plan_migration, validate_migration
from .migration_execution import MigrationExecutor
from .models import AccountModeError, Book, MIN_BATCH_NOTIONAL, MIN_OPEN_LEVERAGE, SYMBOLS, TIERS, TradingError, dec, leverage_candidates, migration_margin_limit, minimum_open_leverage, next_leverage, opening_margin_limit, plan_pair, positive, wire
from .paper import DemoMarket, PaperBroker
from .store import dumps

LOG = logging.getLogger("aster.trading")
MAX_ACCOUNTS = 8
CYCLE_SIGNAL_MAX_AGE = 3
CYCLE_SIGNAL_MIN_INTERVAL = 1
CYCLE_HOT_POLL_INTERVAL = 2
ACCOUNT_LIST_INTERVAL = 1
CAPACITY_POLL_INTERVAL = 2
CAPACITY_MONITOR_RESERVE = len(SYMBOLS) * 2 * 60 // CAPACITY_POLL_INTERVAL
PUBLIC_POLL_ALLOWANCE = CAPACITY_MONITOR_RESERVE + 120 + len(SYMBOLS) * DEPTH_WEIGHT * 60 // DEPTH_RESYNC_INTERVAL
PRIORITY_TIERS = (10, 20)
DEFAULT_POLICY = {"symbols": list(SYMBOLS), "threshold": "10000", "order_notional": "1000", "margin_limit": "0.5",
                  "spread_limit": "0.0005", "min_open_leverage": MIN_OPEN_LEVERAGE, "ordinary_symbol": "all"}
EDITABLE_POLICY_FIELDS = {"threshold", "order_notional", "margin_limit", "min_open_leverage", "ordinary_symbol"}


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
    policy = {"min_open_leverage": MIN_OPEN_LEVERAGE, "ordinary_symbol": "all", **policy}
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
    ordinary_selection(account)
    account["migration"] = validate_migration(account.get("migration", {**DEFAULT_MIGRATION}))
    account["cycle"] = validate_cycle(account.get("cycle"))
    if account["cycle"]["enabled"] and account["migration"]["enabled"]:
        raise TradingError("同一账户不能同时启用多空循环和仓位迁移")
    return account


def snapshot_json(snapshot, symbols):
    result = asdict(snapshot)
    result.pop("brackets", None)
    result.pop("fees", None)
    result.pop("current_leverage_caps", None)
    result.pop("cycle_cap_cached_at", None)
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
        self.cycle_quote_backoff = {}
        self.cycle_market_updates, self.cycle_market_order, self.cycle_signal_seen = {}, {}, {}
        self.cycle_signals, self.cycle_signal_ready, self.cycle_signal_after = {}, {}, {}
        self.cycle_signal_deferred = set()
        self.cycle_hot_wakes, self.cycle_hot_waiting = set(), set()
        self.cycle_hot_backoff = {}
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
        # Background refresh and trading may first reach an account together.
        # Construction is local; serialize registration, never an HTTP read.
        with self.lock:
            if aid in self.brokers:
                return self.brokers[aid]
            if account["mode"] == "paper":
                self.brokers[aid] = PaperBroker(aid, self.market, self.store)
            else:
                if self.demo:
                    raise TradingError("模拟环境不能添加或连接实盘账户")
                creds = credentials_for(account["env_prefix"])
                signer = creds["signer"].lower()
                user = creds["user"].lower()
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

    def wake_cycle_hot_data(self, account_id):
        """A stream callback only queues refresh; it never takes the trade lock."""
        if self.shutdown.is_set():
            return
        with self.lock:
            self.cycle_hot_wakes.add(account_id)
        self.scheduler_event.set()

    def cycle_hot_ready(self, account_id):
        with self.lock:
            self.cycle_hot_waiting.discard(account_id)
            self.cycle_signal_seen.pop(account_id, None)
            self.cycle_signal_ready.pop(account_id, None)
        self.scheduler_event.set()

    def revoke_cycle_hot_data(self, account_id, reason):
        with self.lock:
            broker = self.brokers.get(account_id)
        if isinstance(broker, LiveBroker):
            broker.invalidate_cycle_hot_data(reason, refresh_modes=True)
        self.wake_cycle_hot_data(account_id)

    def poll_cycle_hot_data(self, account_id):
        """Refresh independently of the account execution slot and quote trigger."""
        account = self.store.account(account_id)
        with self.lock:
            existing = self.brokers.get(account_id)
        active = (account and account["mode"] == "live" and account["enabled"]
                  and account.get("cycle", {}).get("enabled") and self.live_allowed(account)
                  and not self.shutdown.is_set())
        if not active:
            if isinstance(existing, LiveBroker):
                existing.stop_cycle_hot_data()
            return 30
        broker = self.broker(account)
        broker.start_cycle_hot_data([account["cycle"]["symbol"]],
                                    on_invalidate=lambda: self.wake_cycle_hot_data(account_id))
        if self.store.intent(account_id) or self.store.get("post_fill_check:" + account_id):
            broker.invalidate_cycle_hot_data("本账户未完成批次正在核对")
            with self.lock:
                self.cycle_hot_backoff[account_id] = time.monotonic() + CYCLE_HOT_POLL_INTERVAL
            return CYCLE_HOT_POLL_INTERVAL
        try:
            # A hot refresh spends ordinary quota only. Keep room for one
            # batch, in addition to the existing monitoring/repair reserves.
            budget = getattr(broker.api, "budget", None)
            if budget is not None:
                budget.require_available(
                    broker.cycle_snapshot_weight([account["cycle"]["symbol"]], fresh_modes=True) + 5)
            published = broker.refresh_cycle_hot_snapshot()
            latest = self.store.account(account_id)
            if (self.shutdown.is_set() or not latest or not latest["enabled"] or latest.get("cycle") != account.get("cycle")
                    or self.store.intent(account_id) or self.store.get("post_fill_check:" + account_id)):
                broker.invalidate_cycle_hot_data("账户或批次在后台更新期间发生变化", refresh_modes=True)
                return CYCLE_HOT_POLL_INTERVAL
            if published:
                self.cycle_hot_ready(account_id)
            return CYCLE_HOT_POLL_INTERVAL
        except HotAccountUnavailable:
            with self.lock:
                self.cycle_hot_backoff[account_id] = time.monotonic() + CYCLE_HOT_POLL_INTERVAL
            return CYCLE_HOT_POLL_INTERVAL
        except AccountModeError as exc:
            broker.invalidate_cycle_hot_data("账户模式不符合要求", refresh_modes=True)
            # The network read is over; serialize the persisted pause with
            # account controls without holding the execution lock during I/O.
            with self.account_lock(account_id):
                latest = self.store.account(account_id)
                if latest and latest["enabled"] and latest.get("cycle") == account.get("cycle"):
                    self.store.pause_account(latest, str(exc))
                    self.view(account_id, status="attention", reason=str(exc))
                    self.cycle_view(latest, phase="attention", reason=str(exc))
                    with self.lock:
                        self.accounts_generation += 1
            return 30
        except (TradingError, KeyError, ValueError, TypeError) as exc:
            broker.invalidate_cycle_hot_data("账户后台更新暂不可用")
            delay = max(CYCLE_HOT_POLL_INTERVAL, getattr(exc, "retry_after", 0))
            with self.lock:
                self.cycle_hot_backoff[account_id] = time.monotonic() + delay
            return delay

    def poll_cycle_history(self, account_id):
        """Only completed batches are backfilled; new orders read the local ledger."""
        account = self.store.account(account_id)
        if (not account or account["mode"] != "live" or not account["enabled"]
                or not account.get("cycle", {}).get("enabled") or not self.live_allowed(account)
                or self.shutdown.is_set()):
            return 30
        if self.store.intent(account_id) or self.store.get("post_fill_check:" + account_id):
            return 2
        broker = self.broker(account)
        executor = CycleExecutor(self.store, broker, self.market)
        backlog = self.store.cycle_volume_backlog(account_id, limit=4, since=max(0, time.time() - 86400))
        if not backlog:
            backlog = self.store.cycle_volume_backlog(account_id, limit=1, since=0)
        try:
            for intent in backlog:
                if self.shutdown.is_set() or not executor.sync_volume(account, intent):
                    return 5
            if backlog:
                self.cycle_hot_ready(account_id)
            return 5 if backlog else 10
        except (TradingError, KeyError, ValueError, TypeError) as exc:
            return max(5, getattr(exc, "retry_after", 0))

    def cycle_book(self, symbol):
        return self.market.cycle_book(symbol) if isinstance(self.market, MarketData) else self.market.book(symbol)

    def cycle_depth(self, symbol):
        return self.market.cycle_depth(symbol) if isinstance(self.market, MarketData) else self.market.depth(symbol)

    def on_cycle_market_update(self, symbol, source, received_at, received_monotonic):
        """The WS receiver only replaces a bounded signal and wakes scheduling."""
        if self.shutdown.is_set() or symbol not in SYMBOLS or source not in ("bbo", "depth"):
            return
        if any(type(value) not in (int, float) or not math.isfinite(value)
               for value in (received_at, received_monotonic)):
            return
        signal = {"symbol": symbol, "source": source, "received_at": received_at,
                  "received_monotonic": received_monotonic}
        with self.lock:
            # Depth seeding preserves a buffered event's receive time, which
            # can predate BBO while only now making full depth available.
            source_key = (symbol, source)
            previous = self.cycle_market_order.get(source_key)
            if previous is not None and received_monotonic < previous:
                return
            self.cycle_market_order[source_key] = received_monotonic
            self.cycle_market_updates[symbol] = signal
        self.scheduler_event.set()

    @staticmethod
    def fresh_cycle_signal(signal):
        return (isinstance(signal, dict) and signal.get("source") in ("bbo", "depth")
                and signal.get("symbol") in SYMBOLS
                and all(type(signal.get(key)) in (int, float) and math.isfinite(signal[key])
                        for key in ("received_at", "received_monotonic"))
                and -1 <= time.time() - signal["received_at"] <= CYCLE_SIGNAL_MAX_AGE
                and 0 <= time.monotonic() - signal["received_monotonic"] <= CYCLE_SIGNAL_MAX_AGE)

    def cycle_public_hint(self, account):
        """Read local public caches only; private state is checked by the worker."""
        symbol = account.get("cycle", {}).get("symbol")
        if not account.get("enabled") or not account.get("cycle", {}).get("enabled") or symbol not in self.market.rules:
            return None
        if self.store.intent(account["id"]) or self.store.get("post_fill_check:" + account["id"]):
            return None
        with self.lock:
            state = self.views.get(account["id"], {}).get("cycle_state", {})
            if state.get("phase") in ("attention", "daily_limit", "rolling_limit"):
                return None
        if isinstance(self.market, MarketData):
            book, depth = self.market.stream.book(symbol), self.market.depth_stream.snapshot(symbol)
        else:
            book, depth = self.market.book(symbol), self.market.depth(symbol)
        if book is None or depth is None:
            return None
        progress = self.store.get("cycle:" + account["id"])
        hint = cycle_signal_quote(account, progress, book, depth, self.market.rules[symbol], now=time.time())
        return {**hint, "depth": depth, "checked_at": time.time()} if hint else None

    def cycle_wake_candidates(self, accounts, pending_keys=()):
        """Coalesce updates; wake once per public-condition opportunity."""
        with self.lock:
            updates = self.cycle_market_updates.copy()
        live_ids = {account["id"] for account in accounts}
        self.cycle_signal_deferred.intersection_update(live_ids)
        for mapping in (self.cycle_signals, self.cycle_signal_seen, self.cycle_signal_ready, self.cycle_signal_after):
            for aid in set(mapping) - live_ids:
                mapping.pop(aid, None)
        for account in accounts:
            aid = account["id"]
            signal = updates.get(account.get("cycle", {}).get("symbol"))
            if not self.fresh_cycle_signal(signal) or not account.get("enabled") or not account.get("cycle", {}).get("enabled"):
                self.cycle_signals.pop(aid, None)
                self.cycle_signal_ready.pop(aid, None)
                continue
            # Leave the latest signal unseen until this account's existing work
            # finishes. No signal is ever allowed to create a second worker.
            if "account:" + aid in pending_keys:
                identity = (signal["symbol"], signal["source"], signal["received_monotonic"], self.accounts_generation)
                if self.cycle_signal_seen.get(aid) != identity:
                    self.cycle_signal_deferred.add(aid)
                continue
            with self.lock:
                backoff = self.account_backoff.get(aid, 0)
                blocked = time.monotonic() < backoff and self.cycle_quote_backoff.get(aid) != backoff
            if blocked:
                self.cycle_signals.pop(aid, None)
                self.cycle_signal_ready.pop(aid, None)
                continue
            identity = (signal["symbol"], signal["source"], signal["received_monotonic"], self.accounts_generation)
            if self.cycle_signal_seen.get(aid) == identity:
                continue
            self.cycle_signal_seen[aid] = identity
            try:
                hint = self.cycle_public_hint(account)
            except (TradingError, KeyError, ValueError, TypeError, OverflowError):
                hint = None
            if hint is None:
                self.cycle_signal_ready.pop(aid, None)
                self.cycle_signals.pop(aid, None)
                continue
            opportunity = (signal["symbol"], hint["phase"], self.accounts_generation)
            if self.cycle_signal_ready.get(aid) != opportunity or aid in self.cycle_signals:
                self.cycle_signals[aid] = {**signal, **hint}
            self.cycle_signal_ready[aid] = opportunity
        now = time.monotonic()
        return {aid: signal for aid, signal in self.cycle_signals.items()
                if self.fresh_cycle_signal(signal) and now >= self.cycle_signal_after.get(aid, 0)}

    @staticmethod
    def cycle_waits_for_market(exc):
        if not isinstance(exc, CycleConditionError) or isinstance(exc, DailyVolumeLimitError):
            return False
        diagnostic = getattr(exc, "diagnostic", None)
        if not isinstance(diagnostic, dict):
            return False
        if diagnostic.get("code") in ("reference_depth", "reference_spread", "close_depth", "close_spread"):
            return True
        if diagnostic.get("code") != "cycle_minimum_order":
            return False
        checks = diagnostic.get("checks")
        failed = [row.get("code") for row in checks if isinstance(row, dict) and row.get("passed") is False] if isinstance(checks, list) else []
        public = {"exchange_min_qty", "exchange_min_notional", "quantity_step", "exchange_max_qty",
                  "depth_buy_quantity", "depth_sell_quantity", "configured_min_notional",
                  "configured_max_notional", "order_spread"}
        return bool(failed) and all(code in public for code in failed)

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
                if account.get("migration", {}).get("enabled"):
                    # Depth reads share the WS book; retain private rechecks.
                    return 300
                cycle_cost = 240 if account.get("cycle", {}).get("enabled") else 0
                markets = ordinary_add_symbols(account)
                if not markets:
                    return cycle_cost
                positions = self.views.get(account["id"], {}).get("snapshot", {}).get("positions", [])
                minimum = minimum_open_leverage(account["policy"])
                for symbol in markets:
                    pair = [p for p in positions if p["symbol"] == symbol]
                    if len(pair) != 2:
                        return cycle_cost + 300  # Cold start may confirm an upgrade.
                    leverage = pair[0]["leverage"]
                    caps = self.markets.get(symbol, {}).get("capacities", {})
                    threshold = dec(account["policy"]["threshold"])
                    if any(target > leverage and dec(caps.get(str(target), "0")) > threshold for target in leverage_candidates(minimum)):
                        return cycle_cost + 300
                return cycle_cost + 120
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
                # Executors can request fewer markets than the combined worker.
                # Live responses omit unrequested flat symbols; freshness alone
                # does not prove this snapshot covers every requested market.
                for symbol in symbols:
                    snapshot.pair(symbol)
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
        """Keep the strategy's BBO indicator independent of display depth."""
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

    def poll_depth(self, symbol):
        """Display depth cannot hold up the shared capacity feed."""
        if self.shutdown.is_set():
            return DEPTH_POLL_INTERVAL
        try:
            depth = self.market.depth(symbol)
            depth.require_fresh()
            display = depth.display()
            depth.require_fresh()
            with self.lock:
                row = self.markets.setdefault(symbol, {})
                row["depth"] = display
                row.pop("depth_error", None)
            return DEPTH_POLL_INTERVAL
        except (TradingError, KeyError, ValueError, TypeError) as exc:
            with self.lock:
                row = self.markets.setdefault(symbol, {})
                # Preserve the last sample as visibly stale context on failures.
                row["depth_error"] = str(exc) if isinstance(exc, TradingError) else "深度数据格式异常"
            return max(DEPTH_POLL_INTERVAL, getattr(exc, "retry_after", 0))

    def wake_capacity_accounts(self, symbol, capacities, checked_at, accounts):
        """Merge availability edges without polling private accounts at the feed cadence."""
        fresh = -1 <= time.time() - checked_at <= 8
        by_id = {a["id"]: a for a in accounts}
        with self.lock:
            ids = set(by_id) | {aid for aid, market in self.priority_levels if market == symbol}
            for aid in ids:
                account = by_id.get(aid)
                levels = ()
                if fresh and account and account["enabled"] and self.live_allowed(account) and account.get("migration", {}).get("enabled"):
                    if symbol in ("SPCXUSD1", "CLUSD1") and capacities.get(5, dec(0)) > 0:
                        levels = (5,)
                elif fresh and account and account["enabled"] and self.live_allowed(account) and symbol in ordinary_add_symbols(account):
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
        if isinstance(batch, dict) and batch.get("kind") == "migration":
            snapshot.pair(batch["symbol"])
            limit = migration_margin_limit(account["policy"])
        elif isinstance(batch, dict) and batch.get("leverage") in (10, 20):
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

    def migration_progress(self, account, snapshot):
        key = "migration:" + account["id"]
        progress = self.store.get(key)
        configured_run = account.get("migration_run_id")
        if not progress or (configured_run and configured_run != progress.get("run_id")):
            long, short = snapshot.require_ready("XAUUSD1")
            quantities = {"LONG": wire(long.qty), "SHORT": wire(short.qty)}
            progress = {"run_id": configured_run or uuid.uuid4().hex, "started_at": time.time(),
                        "updated_at": time.time(), "source_leverage": long.leverage,
                        "source_initial_qty": quantities.copy(), "source_remaining_qty": quantities.copy(),
                        "migrated_notional": {"LONG": "0", "SHORT": "0"},
                        "cumulative_notional_delta": {"LONG": "0", "SHORT": "0"},
                        "completed_batches": 0, "phase": "waiting", "reason": "等待符合条件的迁移目标"}
            self.store.put(key, progress)
        return progress

    def migration_view(self, account, **updates):
        progress = self.store.get("migration:" + account["id"]) or {}
        with self.lock:
            previous = self.views.get(account["id"], {}).get("migration_state", {})
            if previous.get("run_id") != progress.get("run_id"):
                previous = {}
            progress = {**previous, **progress, **updates, "updated_at": time.time()}
        self.view(account["id"], migration_state=progress)
        if "reason" in updates:
            phase = updates.get("phase", "waiting")
            self.view(account["id"], reason=updates["reason"], status="running" if phase == "complete" else phase)

    def tick_migration(self, account, snapshot, broker):
        aid = account["id"]
        progress = self.migration_progress(account, snapshot)
        source = "XAUUSD1"
        long, short = snapshot.require_ready(source)
        actual = {"LONG": wire(long.qty), "SHORT": wire(short.qty)}
        if any(dec(actual[side]) != dec(progress["source_remaining_qty"][side]) for side in actual):
            reason = "XAU 实仓与已核对迁移记录不一致，已暂停；请核对外部交易或仓位变化"
            self.store.pause_account(account, reason)
            self.migration_view(account, phase="attention", reason=reason, source_remaining_qty=actual)
            return 5
        if not long.qty and not short.qty:
            progress.update(phase="complete", reason="XAU 多空仓位已全部迁出；迁移模式保持开启，普通新增已停止", updated_at=time.time())
            self.store.put("migration:" + aid, progress)
            self.migration_view(account, **progress)
            return 5
        for symbol in account["policy"]["symbols"]:
            self.strategy(aid, symbol, "迁移模式已开启，普通新增已停止", "paused")
        targets, reasons = [], []
        for target in ("SPCXUSD1", "CLUSD1"):
            try:
                caps = self.capacities(target)
                if caps.get(5, dec(0)) <= 0:
                    raise TradingError("5x 公开额度不足")
                targets.append(target)
            except TradingError as exc:
                reasons.append(f"{target}：{exc}")
        if not targets:
            self.migration_view(account, phase="waiting", reason="；".join(reasons))
            return 5
        if isinstance(broker, LiveBroker):
            seed_weight = self.market.depth_weight([source, *targets])
            if seed_weight:
                broker.api.budget.require_available(seed_weight)
        # Shared snapshots are immutable; missing stream seeds may perform REST.
        with ThreadPoolExecutor(max_workers=3, thread_name_prefix="migration-depth") as pool:
            jobs = {symbol: pool.submit(self.market.depth, symbol) for symbol in [source, *targets]}
            depths = {}
            for symbol, job in jobs.items():
                try:
                    depths[symbol] = job.result()
                except TradingError as exc:
                    reasons.append(f"{symbol}：{exc}")
        if source not in depths:
            self.migration_view(account, phase="waiting", reason="；".join(reasons))
            return 5
        source_book = self.market.book(source)
        candidates = []
        for target in targets:
            if target not in depths:
                continue
            try:
                book = self.market.book(target)
                plan = plan_migration(account, snapshot, source_book, book, depths[source], depths[target],
                                      self.market.rules[source], self.market.rules[target],
                                      {str(k): v for k, v in self.capacities(target).items()}, progress["source_leverage"], progress)
                candidates.append((plan, book))
            except TradingError as exc:
                reasons.append(f"{target}：{exc}")
        candidates.sort(key=lambda entry: (getattr(entry[0], "spread_exact", entry[0].spread), 0 if entry[0].target_symbol == "SPCXUSD1" else 1))
        if not candidates:
            self.migration_view(account, phase="residual" if any("尾仓" in r for r in reasons) else "waiting", reason="；".join(reasons))
            return 5
        plan, target_book = candidates[0]
        target = plan.target_symbol
        if isinstance(broker, LiveBroker):
            # Both migration execution and leverage changes reload private modes.
            # WS depth requires no recurring REST allowance, but these reads do.
            broker.api.budget.require_available(
                broker.snapshot_weight(migration_symbols(account), fresh_modes=True) + 5)
        self.migration_view(account, phase="executing", reason=f"准备迁移至 {target} {plan.target_leverage}x",
                            target_symbol=target, required_leverage=plan.target_leverage, target_leverage=snapshot.pair(target)[0].leverage)

        def source_unchanged(fresh):
            if self.shutdown.is_set() or not self.live_allowed(account):
                raise TradingError("迁移新增执行已停止")
            fresh.require_ready(source)
            pair = fresh.pair(source)
            if any(position.qty != dec(actual[side]) for side, position in zip(("LONG", "SHORT"), pair)):
                raise TradingError("XAU 仓位已变化，等待重新核对")
            if pair[0].leverage > plan.target_leverage:
                raise TradingError("XAU 杠杆已提高，目标需要重新评估")

        if snapshot.pair(target)[0].leverage < plan.target_leverage:
            def before_leverage(fresh):
                source_unchanged(fresh)
                target_book.require_fresh()
                caps = self.capacities(target)
                amount = sum(plan.target_notionals.values(), dec(0))
                existing = sum((p.qty * target_book.mark for p in fresh.pair(target)), dec(0))
                if caps.get(5, dec(0)) < amount or caps.get(plan.target_leverage, dec(0)) < existing + amount:
                    raise TradingError("迁移目标额度已变化，等待重新评估")
            reason = Executor(self.store, broker, self.market).leverage(
                account, target, snapshot.pair(target)[0].leverage, plan.target_leverage,
                before_submit=before_leverage, purpose="migration", symbols=migration_symbols(account))
            self.migration_view(account, phase="reconciling", reason=reason)
            return 5

        def before_open(fresh):
            source_unchanged(fresh)
            current_source_depth, current_target_depth = self.market.depth(source), self.market.depth(target)
            current_source_book, current_target_book = self.market.book(source), self.market.book(target)
            current = plan_migration(account, fresh, current_source_book, current_target_book,
                                     current_source_depth, current_target_depth,
                                     self.market.rules[source], self.market.rules[target],
                                     {str(k): v for k, v in self.capacities(target).items()}, progress["source_leverage"], progress)
            if (current.target_qty != plan.target_qty or current.source_quantities != plan.source_quantities
                    or current.target_leverage != plan.target_leverage or current.source_leverage != plan.source_leverage):
                raise TradingError("迁移计划因账户或额度变化需要重新计算")
        executor = MigrationExecutor(self.store, broker, self.market)
        reason = executor.start(account, snapshot, plan, run_id=progress["run_id"], before_submit=before_open)
        pending = self.store.intent(aid)
        self.migration_view(account, phase="attention" if pending and pending["status"] == "attention" else "reconciling" if pending else "waiting", reason=reason)
        if not pending:
            after = self.completed_snapshot(executor, broker, migration_symbols(account))
            self.view(aid, snapshot=snapshot_json(after, migration_symbols(account)))
            self.check_post_fill_occupancy(account, after)
        return 5

    def cycle_view(self, account, **updates):
        saved = self.store.get("cycle:" + account["id"]) or {}
        # A fresh state transition or a different unstructured failure must not
        # leave an older condition's numbers attached to the new reason.
        updates.setdefault("diagnostic", None)
        self.view(account["id"], cycle_state={**saved, **updates})

    def cycle_volume_state(self, account, now=None, *, symbol=None):
        symbol = validate_cycle(account.get("cycle"))["symbol"] if symbol is None else symbol
        now = time.time() if now is None else now
        daily = self.store.cycle_daily_volume(account["id"], now=now, symbol=symbol)
        rolling = self.store.cycle_rolling_volume(account["id"], now=now, symbol=symbol)
        limit = dec(validate_cycle(account.get("cycle"))["daily_volume_limit"])
        # Yesterday's unresolved fills can still consume the rolling allowance.
        backlog = self.store.cycle_volume_backlog(account["id"], limit=1, since=max(0, rolling["window_start"]), symbol=symbol)
        def allowance(record):
            used = Fraction(dec(record["volume"]))
            remaining = max(Fraction(0), Fraction(limit) - used)
            return {**record, "limit": wire(limit), "remaining": wire(remaining) if limit else None,
                    "reached": bool(limit and used >= limit), "sync_pending": bool(backlog),
                    "error": backlog[0].get("volume_error") if backlog else None}
        daily, rolling = allowance(daily), allowance(rolling)
        daily["effective_remaining"] = wire(min(dec(daily["remaining"]), dec(rolling["remaining"]))) if limit else None
        return {"daily_volume": daily, "rolling_volume": rolling}

    def cycle_open_allowances(self, account):
        state = self.cycle_volume_state(account)
        daily, rolling = state["daily_volume"], state["rolling_volume"]
        if daily["sync_pending"]:
            if account["mode"] == "live":
                raise HotAccountUnavailable("循环成交明细正在后台同步，完成后再开仓")
            raise TradingError("循环成交明细仍在同步，完成 UTC 日及滚动 24 小时累计量核对后再开仓")
        def quota_error(error_type, title, code):
            checks = [{"code": key, "label": label, "actual": diagnostic_number(record["volume"]),
                       "required": "< " + diagnostic_number(record["limit"]), "unit": "USD1",
                       "passed": not record["reached"]}
                      for key, label, record in (("utc_volume", "UTC 当日成交量", daily),
                                                 ("rolling_volume", "滚动 24 小时成交量", rolling))]
            return diagnostic_error(code, title, symbol=account["cycle"]["symbol"], phase="open",
                                    checked_at=rolling["window_end"], checks=checks,
                                    context=[{"label": "UTC 成交日", "value": daily["utc_date"]},
                                             {"label": "当日剩余额度", "value": daily["remaining"], "unit": "USD1"},
                                             {"label": "近 24 小时剩余额度", "value": rolling["remaining"], "unit": "USD1"}],
                                    note="两个窗口都要留足本轮开仓及预计平仓的成交额度；平仓与必要补救继续允许。",
                                    error_type=error_type)
        if rolling["reached"] and dec(rolling["volume"]) > dec(daily["volume"]):
            raise quota_error(RollingVolumeLimitError, "已达到滚动 24 小时成交量上限，等待历史成交移出窗口后自动重试", "rolling_volume_limit")
        if daily["reached"]:
            raise quota_error(DailyVolumeLimitError, "已达到 UTC 每日成交量上限，待日额度和滚动 24 小时额度均满足后自动恢复", "daily_volume_limit")
        if rolling["reached"]:
            raise quota_error(RollingVolumeLimitError, "已达到滚动 24 小时成交量上限，等待历史成交移出窗口后自动重试", "rolling_volume_limit")
        return {"daily_remaining": daily["remaining"], "rolling_remaining": rolling["remaining"]}

    def cycle_progress(self, account, snapshot):
        progress = self.store.get("cycle:" + account["id"])
        validate_cycle_positions(account, snapshot, progress)
        config = cycle_config(account, snapshot, progress)
        requested = validate_cycle(account.get("cycle"))
        requested["leverage"] = config["leverage"]
        if progress and validate_cycle(progress.get("config")) == requested:
            # Defaulted fields added by an upgrade do not invalidate an active
            # cycle's ownership or restart its holding timer.
            if progress.get("config") != config:
                progress["config"] = config
                self.store.put("cycle:" + account["id"], progress)
            return progress
        if progress and any(dec(q) for q in progress.get("quantities", {}).values()):
            raise CyclePositionError("多空循环持仓参数已变化，请核对记录")
        progress = {"run_id": uuid.uuid4().hex, "phase": "waiting_open", "config": config,
                    "quantities": {"LONG": "0", "SHORT": "0"}, "opened_at": None,
                    "close_eligible_at": None, "completed_cycles": (progress or {}).get("completed_cycles", 0),
                    "updated_at": time.time(), "reason": "等待价差满足开仓条件"}
        self.store.put("cycle:" + account["id"], progress)
        return progress

    def tick_cycle_account(self, account, broker, pending, *, trigger=None):
        """Advance only the selected cycle; the account worker owns other markets."""
        aid = account["id"]
        config = validate_cycle(account.get("cycle"))
        symbol = pending["symbol"] if pending else config["symbol"]
        executor = CycleExecutor(self.store, broker, self.market)
        if pending:
            if self.live_allowed(account):
                reason = executor.reconcile(account, pending)
                remaining = self.store.intent(aid)
                status = "attention" if remaining and remaining["status"] == "attention" else "reconciling" if remaining else "running" if account["enabled"] else "paused"
            else:
                status, reason = "attention", "服务器未启用实盘执行，保留未完成循环批次等待核对"
            self.view(aid, status=status, reason=reason)
            self.cycle_view(account, **({"phase": status, "reason": reason} if self.store.intent(aid) else {}))
            if executor.last_snapshot is not None:
                self.view(aid, snapshot=snapshot_json(executor.last_snapshot, [symbol]), credential_ready=True)
            return 5
        if not account["enabled"] or self.shutdown.is_set():
            reason = account.get("pause_reason") or "循环已暂停，已有仓位和持仓计时保留"
            phase = "attention" if account.get("pause_reason") else "paused"
            self.view(aid, status=phase, reason=reason)
            self.cycle_view(account, phase=phase, reason=reason)
            return 60
        if not self.live_allowed(account):
            raise TradingError("服务器尚未设置 ASTER_ALLOW_LIVE=1")
        # Paper accounting is local; live history runs on its background worker.
        previous = self.store.get("cycle:" + aid)
        if (not isinstance(broker, LiveBroker) and account["enabled"] and not self.shutdown.is_set() and self.live_allowed(account)
                and (not previous or previous.get("phase", "waiting_open") == "waiting_open")):
            backlog = self.store.cycle_volume_backlog(aid, limit=4, since=max(0, time.time() - 86400))
            if not backlog:
                backlog = self.store.cycle_volume_backlog(aid, limit=1, since=0)
            for unsynced in backlog:
                if not executor.sync_volume(account, unsynced):
                    break
        public_prepare_ms = None
        if trigger is not None:
            # Only inspect local quotes here; dedicated market workers recover
            # a missing stream without delaying the trading worker with REST.
            public_prepare_started = clock_tick()
            self.cycle_book(symbol)
            self.cycle_depth(symbol)
            public_prepare_ms = observed_elapsed(public_prepare_started, clock_tick())
        initial_read_started = clock_tick() if trigger is not None else None
        snapshot = executor.prepare_snapshot(account, symbol)
        if trigger is not None:
            trigger = {**trigger, "pre_submit": {**trigger.get("pre_submit", {}),
                "initial_account_ms": observed_elapsed(initial_read_started, clock_tick())}}
        self.view(aid, snapshot=snapshot_json(snapshot, [symbol]), credential_ready=True)
        snapshot.require_modes([symbol])
        progress = self.cycle_progress(account, snapshot)
        config = progress["config"]
        if not account["enabled"] or self.shutdown.is_set():
            reason = account.get("pause_reason") or "循环已暂停，已有仓位和持仓计时保留"
            phase = "attention" if account.get("pause_reason") else "paused"
            self.view(aid, status=phase, reason=reason)
            self.cycle_view(account, phase=phase, reason=reason)
            return 5
        if not self.live_allowed(account):
            raise TradingError("服务器尚未设置 ASTER_ALLOW_LIVE=1")
        # Fill history is read-only. A failed sync blocks only new exposure;
        # tracked positions must remain eligible for closing and repairs.
        allowances = {}
        if progress.get("phase") == "waiting_open":
            allowances = self.cycle_open_allowances(account)
        retry_at = progress.get("retry_at") or 0
        if progress.get("phase") == "waiting_open" and time.time() < retry_at:
            reason = "上一批开仓未完成，等待冷却后重新检查"
            self.view(aid, status="waiting", reason=reason)
            self.cycle_view(account, phase="waiting_open", reason=reason)
            return max(1, min(30, retry_at - time.time()))
        if progress.get("opened_at") is not None:
            eligible = progress["opened_at"] + progress["config"]["hold_seconds"]
            if time.time() < eligible:
                reason = "双向持仓中，持仓时间到达后等待价差达标平仓"
                self.view(aid, status="running", reason=reason)
                self.cycle_view(account, phase="holding", reason=reason, close_eligible_at=eligible)
                return max(1, min(5, eligible - time.time()))
            self.cycle_view(account, phase="waiting_close", close_eligible_at=eligible,
                            reason="持仓时间已到，等待价差达标平仓")
        else:
            self.cycle_view(account, phase="waiting_open", reason="等待价差达标开仓")
        if isinstance(broker, LiveBroker):
            broker.api.budget.require_available(5)
        planning_started = clock_tick() if trigger is not None else None
        book, depth = self.cycle_book(symbol), self.cycle_depth(symbol)
        plan = plan_cycle(account, snapshot, book, depth, self.market.rules[symbol], progress,
                          **allowances)
        if trigger is not None:
            planning_ms = observed_elapsed(planning_started, clock_tick())
            trigger = {**trigger, "pre_submit": {**trigger.get("pre_submit", {}),
                "planning_ms": (planning_ms + public_prepare_ms
                                if planning_ms is not None and public_prepare_ms is not None else None)}}

        def before_submit(fresh):
            if self.shutdown.is_set() or not self.live_allowed(account):
                raise TradingError("多空循环已停止提交")
            final_book, final_depth = self.cycle_book(symbol), self.cycle_depth(symbol)
            current = plan_cycle(account, fresh, final_book, final_depth,
                                 self.market.rules[symbol], progress,
                                 **(self.cycle_open_allowances(account) if plan.phase == "open" else {}))
            if current.phase != plan.phase or current.qty != plan.qty or current.leverage != plan.leverage:
                raise TradingError("循环计划因账户或盘口变化需要重新计算")
            return {"depth": final_depth, "checked_at": time.time(), "checked_monotonic": time.monotonic(),
                    "quality_plan": current}

        reason = executor.start(account, snapshot, plan, progress, before_submit=before_submit,
                                **({"trigger": trigger} if trigger is not None else {}))
        remaining = self.store.intent(aid)
        status = "attention" if remaining and remaining["status"] == "attention" else "reconciling" if remaining else "running"
        self.view(aid, status=status, reason=reason)
        self.cycle_view(account, spread_bp=wire(plan.spread_bp), spread_checked_at=depth.timestamp,
                        **({"phase": status, "reason": reason} if remaining else {}))
        if executor.last_snapshot is not None:
            self.view(aid, snapshot=snapshot_json(executor.last_snapshot, [symbol]))
        return 5

    def record_cycle_check(self, account, exc):
        diagnostic = getattr(exc, "diagnostic", None)
        phase = diagnostic.get("phase") if isinstance(diagnostic, dict) else None
        if phase not in ("open", "close"):
            progress = self.store.get("cycle:" + account["id"]) or {}
            phase = "close" if progress.get("opened_at") is not None else "open"
        self.store.record_cycle_check(account["id"], account["cycle"]["symbol"], phase,
                                      str(exc), diagnostic=diagnostic)

    def cycle_wait(self, account, exc):
        """A local cycle condition must not suppress ordinary work elsewhere."""
        aid, message = account["id"], str(exc)
        with self.lock:
            previous = self.views.get(aid, {}).get("cycle_state", {}).get("reason")
        if isinstance(exc, CycleConditionError) and not self.store.intent(aid):
            self.record_cycle_check(account, exc)
        elif previous != message:
            self.store.event(aid, "wait", message)
        progress = self.store.get("cycle:" + aid) or {}
        phase = ("rolling_limit" if isinstance(exc, RollingVolumeLimitError) else
                 "daily_limit" if isinstance(exc, DailyVolumeLimitError) else
                 "waiting_close" if progress.get("opened_at") is not None else "waiting_open")
        quota = self.cycle_volume_state(account)["daily_volume"] if isinstance(exc, DailyVolumeLimitError) else {}
        self.cycle_view(account, phase=phase, reason=message,
                        quota_utc_date=quota.get("utc_date"), quota_remaining=quota.get("effective_remaining"),
                        diagnostic=getattr(exc, "diagnostic", None))

    def tick_account(self, account_id, *, cycle_signal=None):
        with self.account_lock(account_id):
            worker_started = clock_tick() if cycle_signal is not None else None
            with self.lock:
                priority = account_id in self.active_priority_accounts
                priority_signals = self.active_priority_signals.pop(account_id, {})
            progressed, consumed_symbol, retry_priority = False, None, False
            if self.shutdown.is_set():
                return 5
            account = self.store.account(account_id)
            if not account:
                return 5
            pending = None
            cycle_context = False
            try:
                if cycle_signal is not None:
                    if (not self.fresh_cycle_signal(cycle_signal)
                            or cycle_signal["symbol"] != account.get("cycle", {}).get("symbol")):
                        return 5
                    cycle_signal = {**cycle_signal, "pre_submit": {
                        "queue_ms": observed_elapsed(cycle_signal["received_monotonic"], worker_started)}}
                    try:
                        ready = self.cycle_public_hint(account)
                    except (TradingError, KeyError, ValueError, TypeError, OverflowError):
                        ready = None
                    if ready is None:
                        return 5
                broker = self.broker(account)
                pending = self.store.intent(account_id)
                cycling = account.get("cycle", {}).get("enabled")
                ordinary_markets = ordinary_add_symbols(account)
                if cycle_signal is not None:
                    if pending or self.store.get("post_fill_check:" + account_id):
                        return 5
                    cycle_context = True
                    if isinstance(broker, LiveBroker):
                        symbol = account["cycle"]["symbol"]
                        # Only the order request spends quota on this hot path.
                        broker.api.budget.require_available(5)
                    return self.tick_cycle_account(account, broker, None, trigger=cycle_signal)
                if pending and pending["kind"] in ("cycle", "cycle_leverage"):
                    cycle_context = True
                    progressed = True
                    return self.tick_cycle_account(account, broker, pending)
                if not pending and cycling and not self.store.get("post_fill_check:" + account_id):
                    cycle_context = True
                    if not ordinary_markets:
                        return self.tick_cycle_account(account, broker, None)
                    try:
                        self.tick_cycle_account(account, broker, None)
                    except (ExchangeError, AccountModeError, CyclePositionError):
                        # Shared budget, transport, and account integrity errors
                        # keep their account-wide stop/backoff behavior.
                        raise
                    except TradingError as exc:
                        if self.store.intent(account_id):
                            raise
                        self.cycle_wait(account, exc)
                    # A cycle may have completed, paused the account, or left an
                    # uncertain batch. Never reuse its pre-mutation account view.
                    account = self.store.account(account_id)
                    if self.store.intent(account_id):
                        progressed = True
                        return 5
                    if not account or not account["enabled"] or self.shutdown.is_set():
                        return 5
                    cycle_context = False
                symbols = migration_symbols(account)
                if cycling:
                    symbols = list(dict.fromkeys([*symbols, account["cycle"]["symbol"]]))
                if pending and (pending["kind"] == "migration" or pending.get("purpose") == "migration"):
                    symbols = list(dict.fromkeys([*symbols, *SYMBOLS]))
                recovery = bool(pending or self.store.get("post_fill_check:" + account_id))
                with self.recovery_budget(broker) if recovery else nullcontext():
                    if isinstance(broker, LiveBroker) and not recovery:
                        broker.api.budget.require_available(broker.snapshot_weight(symbols))
                    snapshot = broker.snapshot(symbols)
                self.view(account_id, snapshot=snapshot_json(snapshot, symbols), credential_ready=True)
                snapshot.require_modes(symbols)
                if self.shutdown.is_set():
                    return 5
                if (not pending or pending["kind"] != "migration") and self.store.get("post_fill_check:" + account_id) and self.check_post_fill_occupancy(account, snapshot):
                    return 5
                pause_reason = account.get("pause_reason") if not account["enabled"] else None
                self.view(account_id, status="running" if account["enabled"] else "attention" if pause_reason else "paused",
                          reason="策略运行中" if account["enabled"] else pause_reason or "策略已暂停")
                executor = (MigrationExecutor if pending and pending["kind"] == "migration" else Executor)(self.store, broker, self.market)
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
                    if pending["kind"] == "migration":
                        still_pending = self.store.intent(account_id)
                        phase = status if still_pending else (self.store.get("migration:" + account_id) or {}).get("phase", "waiting")
                        self.migration_view(account, phase=phase, reason=reason)
                        if not still_pending:
                            after = self.completed_snapshot(executor, broker, symbols)
                            self.view(account_id, snapshot=snapshot_json(after, symbols))
                            self.check_post_fill_occupancy(account, after)
                    elif pending["kind"] == "pair" and not self.store.intent(account_id):
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
                    if account.get("migration", {}).get("enabled"):
                        self.migration_view(account, phase="attention" if account.get("pause_reason") else "paused", reason=account.get("pause_reason") or "账户已暂停，迁移进度已保存")
                    for symbol in account["policy"]["symbols"]:
                        self.strategy(account_id, symbol, "策略已暂停", "paused")
                    if snapshot.equity > 0:
                        self.store.finish_campaign(account, "策略已暂停", snapshot.ratio)
                    return 5
                if not self.live_allowed(account):
                    raise TradingError("服务器尚未设置 ASTER_ALLOW_LIVE=1")
                if account.get("migration", {}).get("enabled"):
                    return self.tick_migration(account, snapshot, broker)
                policy = account["policy"]
                minimum = minimum_open_leverage(policy)
                markets = ordinary_add_symbols(account)
                for symbol, reason in ordinary_add_blocks(account).items():
                    self.strategy(account_id, symbol, reason, "disabled")
                if not markets:
                    return 5
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
                                broker.api.budget.require_available(broker.snapshot_weight(account["policy"]["symbols"], fresh_modes=True) + 1)
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
                        after = self.completed_snapshot(executor, broker, symbols)
                        unresolved = self.store.intent(account_id)
                        phase = ("attention" if unresolved["status"] == "attention" else "reconciling") if unresolved else "filled"
                        self.view(account_id, snapshot=snapshot_json(after, symbols), reason=reason, status=phase if unresolved else "running")
                        after.require_modes(symbols)
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
            except HotAccountUnavailable as exc:
                # Missing hot data queues background work; it must never cause
                # an on-demand query or a long account-wide network backoff.
                with self.lock:
                    self.cycle_hot_waiting.add(account_id)
                    self.cycle_signal_ready.pop(account_id, None)
                self.wake_cycle_hot_data(account_id)
                self.view(account_id, status="waiting", reason=str(exc), credential_ready=True)
                progress = self.store.get("cycle:" + account_id) or {}
                self.cycle_view(account, phase="waiting_close" if progress.get("opened_at") is not None else "waiting_open",
                                reason=str(exc))
                return 1
            except (TradingError, KeyError, ValueError, TypeError) as exc:
                message = str(exc) if isinstance(exc, TradingError) else "账户响应格式异常，已停止本轮操作"
                with self.lock:
                    old_reason = self.views.get(account_id, {}).get("reason")
                    log_wait = not isinstance(exc, BudgetWait) or time.monotonic() - self.budget_wait_events.get(account_id, -1e9) >= 60
                    if isinstance(exc, BudgetWait) and old_reason != message and log_wait:
                        self.budget_wait_events[account_id] = time.monotonic()
                if cycle_context and isinstance(exc, CycleConditionError) and not self.store.intent(account_id):
                    self.record_cycle_check(account, exc)
                elif old_reason != message and log_wait:
                    self.store.event(account_id, "wait" if isinstance(exc, (BudgetWait, DailyVolumeLimitError)) else "error", message)
                status = "waiting" if isinstance(exc, (BudgetWait, DailyVolumeLimitError)) else "error"
                if isinstance(exc, (AccountModeError, CyclePositionError)):
                    self.store.pause_account(account, message)
                    pending = self.store.intent(account_id)
                    if pending:
                        pending.update(status="attention", last_error=message)
                        self.store.save_intent(pending)
                    for symbol in account["policy"]["symbols"]:
                        self.strategy(account_id, symbol, message, "attention")
                    status = "attention"
                self.view(account_id, status=status, reason=message, credential_ready=account_id in self.brokers)
                if account.get("migration", {}).get("enabled") or (pending and pending.get("kind") == "migration"):
                    self.migration_view(account, phase="attention" if status == "attention" else "waiting", reason=message)
                if cycle_context or (pending and pending.get("kind") in ("cycle", "cycle_leverage")) or (
                        status == "attention" and account.get("cycle", {}).get("enabled")):
                    progress = self.store.get("cycle:" + account_id) or {}
                    phase = "rolling_limit" if isinstance(exc, RollingVolumeLimitError) else "daily_limit" if isinstance(exc, DailyVolumeLimitError) else "attention" if status == "attention" else "waiting_close" if progress.get("opened_at") is not None else "waiting_open"
                    quota = self.cycle_volume_state(account)["daily_volume"] if isinstance(exc, DailyVolumeLimitError) else {}
                    self.cycle_view(account, phase=phase, reason=message,
                                    quota_utc_date=quota.get("utc_date"), quota_remaining=quota.get("effective_remaining"),
                                    diagnostic=getattr(exc, "diagnostic", None))
                delay = max(10, getattr(exc, "retry_after", 0))
                with self.lock:
                    self.account_backoff[account_id] = time.monotonic() + delay
                    if cycle_context and self.cycle_waits_for_market(exc):
                        self.cycle_quote_backoff[account_id] = self.account_backoff[account_id]
                    else:
                        self.cycle_quote_backoff.pop(account_id, None)
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
            pending = self.store.intent(account_id)
            stop_migration = (isinstance(changes, dict) and set(changes) == {"migration"}
                              and isinstance(changes["migration"], dict)
                              and set(changes["migration"]) == {"enabled"}
                              and changes["migration"]["enabled"] is False)
            if stop_migration:
                account.update(enabled=False, migration={**account.get("migration", DEFAULT_MIGRATION), "enabled": False})
                self.store.save_account(account)
                self.revoke_cycle_hot_data(account_id, "账户已暂停")
                with self.lock:
                    self.wake_accounts.add(account_id)
                    if pending:
                        self.urgent_accounts.add(account_id)
                    self.accounts_generation += 1
                reason = "迁移已停止，账户已暂停" + ("；已提交批次继续核对" if pending else "")
                self.view(account_id, status="reconciling" if pending else "paused", reason=reason)
                self.store.event(account_id, "control", reason)
                return
            if account["enabled"] or pending:
                raise TradingError("请先暂停策略并等待当前批次完成")
            progress = self.store.get("cycle:" + account_id) or {}
            if any(dec(q) for q in progress.get("quantities", {}).values()):
                raise TradingError("多空循环仍有持仓，请恢复循环并等待平仓完成后修改参数或退出循环模式")
            if not isinstance(changes, dict) or not changes or set(changes) - (EDITABLE_POLICY_FIELDS | {"migration", "cycle"}):
                raise TradingError("请选择有效的策略配置字段")
            policy_changes = {key: value for key, value in changes.items() if key not in ("migration", "cycle")}
            if any(value is None or (key != "min_open_leverage" and not isinstance(value, str)) for key, value in policy_changes.items()):
                raise TradingError("策略配置字段类型无效")
            if "migration" in changes:
                migration_changes = changes["migration"]
                if not isinstance(migration_changes, dict) or not migration_changes:
                    raise TradingError("请提供非空的迁移配置字段")
                migration_was_enabled = account.get("migration", DEFAULT_MIGRATION)["enabled"]
                account["migration"] = validate_migration({**account.get("migration", DEFAULT_MIGRATION), **migration_changes})
                if account["migration"]["enabled"] and not migration_was_enabled:
                    account["migration_run_id"] = uuid.uuid4().hex
            if "cycle" in changes:
                if not isinstance(changes["cycle"], dict) or not changes["cycle"]:
                    raise TradingError("请提供非空的多空循环配置字段")
                account["cycle"] = validate_cycle({**account.get("cycle", DEFAULT_CYCLE), **changes["cycle"]})
            account["policy"] = {**account["policy"], **policy_changes}
            validate_account(account)
            if "min_open_leverage" in changes:
                account.pop("leverage_setting_required", None)
            self.store.save_account(account)
            self.revoke_cycle_hot_data(account_id, "账户配置已更新")
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
                cycling = account.get("cycle", {}).get("enabled")
                ordinary_markets = ordinary_add_symbols(account)
                ordinary_active = bool(ordinary_markets) and not account.get("migration", {}).get("enabled")
                if account.get("leverage_setting_required") and (not cycling or ordinary_active):
                    raise TradingError("请先在 5x、10x、20x 中保存最低开仓杠杆设置")
                if not self.live_allowed(account):
                    raise TradingError("服务器尚未启用实盘执行（ASTER_ALLOW_LIVE=1）")
                if ordinary_active and dec(account["policy"]["order_notional"]) < MIN_BATCH_NOTIONAL:
                    raise TradingError("单批每边上限低于固定最低批次金额 500 USD1，请先修改策略设置")
                if self.store.intent(account_id):
                    raise TradingError("请先核对未完成批次")
                symbols = cycle_symbols(account)
                broker = self.broker(account)
                if cycling:
                    validate_account(account)
                    snapshot = broker.cycle_snapshot(symbols, fresh_modes=True)
                    snapshot.require_fresh()
                    snapshot.require_modes(symbols)
                    if not snapshot.can_trade:
                        raise TradingError("多空循环需要账户交易权限")
                    self.cycle_progress(account, snapshot)
                    if ordinary_markets:
                        symbols = list(dict.fromkeys([*account["policy"]["symbols"], account["cycle"]["symbol"]]))
                        snapshot = broker.snapshot(symbols, fresh_modes=True)
                        snapshot.require_modes(symbols)
                        for symbol in ordinary_markets:
                            snapshot.require_ready(symbol)
                        self.cycle_progress(account, snapshot)
                else:
                    snapshot = broker.snapshot(symbols, fresh_modes=True)
                    for symbol in symbols:
                        snapshot.require_ready(symbol)
                view_updates = {"snapshot": snapshot_json(snapshot, symbols), "credential_ready": True,
                                "strategies": {symbol: {"phase": "waiting", "reason": "策略已启动，等待下一轮检查"}
                                               for symbol in account["policy"]["symbols"]}}
                # High existing occupancy blocks additions in plan_pair, while an
                # authorized increase in leverage can release margin before adding.
            account["enabled"] = enabled
            if enabled:
                account.pop("pause_reason", None)
            self.store.save_account(account)
            self.revoke_cycle_hot_data(account_id, "账户运行状态已更新")
            with self.lock:
                self.wake_accounts.add(account_id)
                self.accounts_generation += 1
                current_cycle = self.views.get(account_id, {}).get("cycle_state")
                if current_cycle is not None:
                    # Clear the stored view too: a pause followed by resume must
                    # not resurrect a pre-pause diagnostic before the next tick.
                    view_updates["cycle_state"] = {**current_cycle, "diagnostic": None}
            self.view(account_id, status="running" if enabled else "attention" if account.get("pause_reason") else "paused",
                      reason="策略运行中" if enabled else account.get("pause_reason") or "策略已暂停", **view_updates)
            self.store.event(account_id, "control", "策略已启动" if enabled else "策略已暂停；已提交批次继续核对")

    def retry(self, account_id):
        with self.account_lock(account_id):
            intent = self.store.intent(account_id)
            if not intent:
                return
            intent["status"] = "pending"
            if intent["kind"] in ("pair", "cycle"):
                intent["repair_attempts"] = 0
            elif intent["kind"] == "migration":
                intent["repair_attempts"] = 0
                intent["attempts"] = {}
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
        state_now = time.time()
        saved_accounts = self.store.accounts()
        events = self.store.events()
        pending_notifications = self.store.pending_notifications()
        migration_records = {a["id"]: (self.store.get("migration:" + a["id"]) or {}, self.store.intent(a["id"])) for a in saved_accounts}
        cycle_records = {a["id"]: self.store.get("cycle:" + a["id"]) or {} for a in saved_accounts}
        cycle_quality = {a["id"]: self.store.get("cycle_execution:" + a["id"]) for a in saved_accounts}
        cycle_selection = {a["id"]: validate_cycle(a.get("cycle"))["symbol"] for a in saved_accounts}
        symbol_volumes = {a["id"]: {symbol: self.cycle_volume_state(a, now=state_now, symbol=symbol)
                                     for symbol in SYMBOLS} for a in saved_accounts}
        cycle_volumes = {a["id"]: symbol_volumes[a["id"]][cycle_selection[a["id"]]] for a in saved_accounts}
        cycle_trades = {a["id"]: self.store.cycle_trade_records(a["id"], limit=100) for a in saved_accounts}
        add_blocks = {a["id"]: ordinary_add_blocks(a) for a in saved_accounts}
        cycle_costs = {}
        for account in saved_accounts:
            aid = account["id"]
            try:
                fills = self.store.cycle_cost_records(aid, now=state_now, limit=100)
                report = calculate_cycle_costs(fills, now=state_now)
                trade_costs = {(row["symbol"], row["trade_id"]): row for row in report["trades"]}
                for trade in cycle_trades[aid]:
                    cost = trade_costs.get((trade["symbol"], trade["trade_id"]))
                    if cost is not None:
                        trade["cost"] = {key: value for key, value in cost.items() if key not in ("symbol", "trade_id")}
                cycle_costs[aid] = calculate_cycle_costs(
                    [fill for fill in fills if fill["symbol"] == cycle_selection[aid]], now=state_now)
            except Exception as exc:
                # Cost reporting is read-only and must not interrupt the account
                # controls or turn a missing calculation into a zero-cost claim.
                LOG.warning("Cycle cost report unavailable (%s)", type(exc).__name__)
                unavailable = {"taker_rate": "0.000125", "taker_rate_percent": "0.0125",
                               "taker_fee": None, "spread_cost": None, "total_cost": None,
                               "unmatched_notional": None, "unmatched_fill_count": None,
                               "complete": False, "error": "成本统计暂不可用，等待重新读取成交记录"}
                cycle_costs[aid] = {"daily": dict(unavailable), "rolling": dict(unavailable)}
        request_budget = self.market.api.budget.snapshot() if isinstance(self.market, MarketData) else None
        with self.lock:
            accounts = [{**a, "risk_limits": {"base": a["policy"]["margin_limit"],
                                            "high_leverage": wire(opening_margin_limit(a["policy"], 10)),
                                            "migration": wire(migration_margin_limit(a["policy"])),
                                            "cycle": wire(cycle_margin_limit(a["policy"]))},
                         **self.views.get(a["id"], {
                "status": "attention" if a.get("pause_reason") else "starting",
                "reason": a.get("pause_reason") or "等待读取账户", "credential_ready": False, "strategies": {}})} for a in saved_accounts]
            for account in accounts:
                # This is selected-account policy, not a global market lock.
                # Recompute from saved configuration so an older view cannot
                # retain a block after the cycle selection changes.
                account["ordinary_add_blocks"] = add_blocks[account["id"]]
                saved, pending = migration_records[account["id"]]
                if account.get("migration_run_id") and account["migration_run_id"] != saved.get("run_id"):
                    saved = {}
                current = account.get("migration_state", {})
                if current.get("run_id") != saved.get("run_id"):
                    current = {}
                migration = {**saved, **current}
                active = pending and (pending["kind"] == "migration" or pending.get("purpose") == "migration")
                if active:
                    migration.update(phase="attention" if pending["status"] == "attention" else "reconciling",
                                     reason=pending.get("last_error") or account["reason"],
                                     active_batch={"stage": pending.get("phase", "leverage"), "source_symbol": "XAUUSD1",
                                                   "target_symbol": pending.get("target_symbol", pending["symbol"])})
                elif not account.get("migration", {}).get("enabled"):
                    migration.update(phase="disabled", reason="迁移已关闭", active_batch=None)
                elif not account["enabled"]:
                    migration.update(phase="attention" if account.get("pause_reason") else "paused",
                                     reason=account.get("pause_reason") or "账户已暂停，迁移进度已保存", active_batch=None)
                elif saved.get("phase") == "complete":
                    migration.update(saved, active_batch=None)
                else:
                    migration.setdefault("phase", "waiting")
                    migration.setdefault("reason", "等待迁移检查")
                    migration["active_batch"] = None
                account["migration_state"] = migration
                saved_cycle = cycle_records[account["id"]]
                current_cycle = account.get("cycle_state", {})
                if current_cycle.get("run_id") != saved_cycle.get("run_id"):
                    current_cycle = {}
                cycle = {**saved_cycle, **current_cycle}
                daily, rolling = (cycle_volumes[account["id"]][key] for key in ("daily_volume", "rolling_volume"))
                # Durable phase/ownership must win after an executor completes.
                if saved_cycle.get("updated_at", 0) > current_cycle.get("updated_at", 0):
                    cycle.update(saved_cycle, diagnostic=None)
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
                    if not cycle.get("opened_at") and rolling["reached"] and dec(rolling["volume"]) > dec(daily["volume"]):
                        cycle.update(phase="rolling_limit", reason="已达到滚动 24 小时成交量上限，等待历史成交移出窗口")
                    elif not cycle.get("opened_at") and daily["reached"]:
                        cycle.update(phase="daily_limit", reason="已达到 UTC 每日成交量上限，待日额度和滚动 24 小时额度均满足后自动恢复")
                    elif not cycle.get("opened_at") and rolling["reached"]:
                        cycle.update(phase="rolling_limit", reason="已达到滚动 24 小时成交量上限，等待历史成交移出窗口")
                    elif cycle.get("phase") in ("daily_limit", "rolling_limit"):
                        previous_remaining = cycle.get("quota_remaining")
                        if daily["effective_remaining"] is None or (previous_remaining is not None and
                                dec(daily["effective_remaining"]) > dec(previous_remaining)):
                            cycle.update(phase="waiting_open", reason="成交额度已释放，等待重新核对开仓条件", diagnostic=None)
                        elif cycle.get("quota_utc_date") != daily["utc_date"]:
                            cycle.update(phase="rolling_limit", reason="UTC 日额度已重置，仍需满足滚动 24 小时开平仓额度")
                    cycle["active_batch"] = None
                if active_cycle or cycle.get("phase") not in ("waiting_open", "waiting_close", "daily_limit", "rolling_limit"):
                    cycle["diagnostic"] = None
                if active_cycle and pending.get("volume_error"):
                    daily = {**daily, "sync_pending": True, "error": pending["volume_error"]}
                    rolling = {**rolling, "sync_pending": True, "error": pending["volume_error"]}
                for volume, window in ((daily, "daily"), (rolling, "rolling")):
                    cost = dict(cycle_costs[account["id"]][window])
                    # An active batch may contain submitted orders whose fills
                    # have not reached the ledger yet, even without an error.
                    sync_pending = bool(volume["sync_pending"] or active_cycle and pending["kind"] == "cycle")
                    cost["sync_pending"] = sync_pending
                    if sync_pending:
                        cost["complete"] = False
                        cost["error"] = volume.get("error") or cost.get("error")
                    volume["cost"] = cost
                cycle["volume_by_symbol"] = symbol_volumes[account["id"]]
                cycle["daily_volume"] = daily
                cycle["rolling_volume"] = rolling
                cycle["execution_quality"] = cycle_quality[account["id"]]
                if cycle.get("opened_at") is not None:
                    cycle["close_eligible_at"] = cycle["opened_at"] + cycle.get("config", account["cycle"])["hold_seconds"]
                account["cycle_state"] = cycle
                account["cycle_trades"] = cycle_trades[account["id"]]
            return json.loads(dumps({"demo": self.demo, "ready": self.ready, "error": self.error,
                "accounts": accounts, "markets": self.markets, "events": events, "updated_at": time.time(), "request_budget": request_budget,
                "notification": {"configured": bool(os.environ.get("FEISHU_WEBHOOK_URL")), "pending": pending_notifications,
                                 "error": self.notification_error or next(iter(self.capacity_notification_errors.values()), None)}}))

    def run(self):
        pending, due = {}, {}
        cycle_job_due = {}
        account_ids, account_generation, accounts_due = [], -1, 0
        schedules, known_accounts, next_ordinary_start = {}, set(), 0
        saved_accounts = []
        # Account refresh and history never occupy an execution worker's slot.
        try:
            if isinstance(self.market, MarketData) and not self.shutdown.is_set():
                self.market.api.budget.configure_capacity_reserve(CAPACITY_MONITOR_RESERVE)
                self.market.set_update_listener(self.on_cycle_market_update)
                try:
                    self.market.start_stream()
                except Exception:
                    LOG.warning("Public quote stream unavailable; using REST quotes")
            with ThreadPoolExecutor(max_workers=3 * MAX_ACCOUNTS + 3 * len(SYMBOLS) + 1, thread_name_prefix="aster") as pool:
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
                                    if key.startswith("account:"):
                                        failed_aid = key.removeprefix("account:")
                                        with self.lock:
                                            self.account_backoff[failed_aid] = time.monotonic() + delay
                                            self.cycle_quote_backoff.pop(failed_aid, None)
                                    elif key.startswith("cycle-data:"):
                                        with self.lock:
                                            self.cycle_hot_backoff[key.removeprefix("cycle-data:")] = time.monotonic() + delay
                                aid = key.removeprefix("account:")
                                with self.lock:
                                    urgent = aid in self.urgent_accounts
                                    self.active_priority_accounts.discard(aid)
                                    self.active_priority_signals.pop(aid, None)
                                seen = self.cycle_signal_seen.get(aid)
                                with self.lock:
                                    latest_signal = self.cycle_market_updates.get(seen[0]) if seen else None
                                newer_signal = latest_signal and (latest_signal["source"], latest_signal["received_monotonic"]) != seen[1:3]
                                if aid in self.cycle_signal_deferred or newer_signal:
                                    self.cycle_signal_ready.pop(aid, None)
                                    self.cycle_signal_seen.pop(aid, None)
                                    self.cycle_signal_deferred.discard(aid)
                                if key in cycle_job_due:
                                    # A fast cycle attempt borrows the existing
                                    # account worker; it never postpones ordinary
                                    # work or consumes another market's wakeup.
                                    due[key] = cycle_job_due.pop(key)
                                else:
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
                        cycle_candidates = self.cycle_wake_candidates(saved_accounts, pending)
                        with self.lock:
                            for aid in list(self.cycle_hot_wakes):
                                key = "cycle-data:" + aid
                                if key not in pending and time.monotonic() >= self.cycle_hot_backoff.get(aid, 0):
                                    due[key] = 0
                                    self.cycle_hot_wakes.discard(aid)
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
                        jobs.update({"depth:" + s: (self.poll_depth, s) for s in SYMBOLS})
                        jobs["notify"] = (self.notify,)
                        live_ids = [a["id"] for a in saved_accounts if a["mode"] == "live"]
                        jobs.update({"cycle-data:" + aid: (self.poll_cycle_hot_data, aid) for aid in live_ids})
                        jobs.update({"cycle-history:" + aid: (self.poll_cycle_history, aid) for aid in live_ids})
                        ordered_accounts = sorted(account_ids, key=lambda aid: (aid not in urgent_accounts,
                            aid not in cycle_candidates, aid not in priority_accounts))
                        jobs.update({"account:" + aid: (self.tick_account, aid) for aid in ordered_accounts})
                        for key, (function, *args) in jobs.items():
                            is_account = key.startswith("account:")
                            aid = key.removeprefix("account:")
                            priority = is_account and aid in priority_accounts
                            cycle_signal = cycle_candidates.get(aid) if is_account and aid not in urgent_accounts else None
                            # Due ordinary work retains its turn and can itself
                            # progress the cycle, so continuous quotes cannot
                            # monopolize this account's shared execution slot.
                            ordinary_due = time.monotonic() >= due.get(key, 0) and time.monotonic() >= next_ordinary_start
                            if priority or ordinary_due:
                                cycle_signal = None
                            if key not in pending and (cycle_signal or priority or time.monotonic() >= due.get(key, 0)):
                                if key.startswith("cycle-data:"):
                                    with self.lock:
                                        if time.monotonic() < self.cycle_hot_backoff.get(key.removeprefix("cycle-data:"), 0):
                                            continue
                                if is_account:
                                    with self.lock:
                                        backoff = self.account_backoff.get(aid, 0)
                                        quote_wait = cycle_signal is not None and self.cycle_quote_backoff.get(aid) == backoff
                                        if time.monotonic() < backoff and not quote_wait:
                                            continue
                                ordinary = is_account and aid in schedules and aid not in urgent_accounts and not priority and cycle_signal is None
                                if ordinary and time.monotonic() < next_ordinary_start:
                                    continue
                                if priority:
                                    with self.lock:
                                        self.active_priority_signals[aid] = self.priority_accounts.pop(aid, {})
                                        self.priority_followups.discard(aid)
                                        self.active_priority_accounts.add(aid)
                                if cycle_signal is not None:
                                    cycle_job_due[key] = due.get(key, 0)
                                    self.cycle_signals.pop(aid, None)
                                    self.cycle_signal_after[aid] = time.monotonic() + CYCLE_SIGNAL_MIN_INTERVAL
                                    pending[key] = pool.submit(function, *args, cycle_signal=cycle_signal)
                                else:
                                    if is_account:
                                        self.cycle_signals.pop(aid, None)
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
                        closing_ids = list(self.brokers)
                    for aid in closing_ids:
                        self.revoke_cycle_hot_data(aid, "交易调度已停止")
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
                    self.market.set_update_listener(None)
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
            with self.lock:
                closing_ids = list(self.brokers)
            for aid in closing_ids:
                self.revoke_cycle_hot_data(aid, "交易服务正在停止")
            if self.thread:
                self.thread.join(timeout=90)
            if not self.thread or not self.thread.is_alive():
                self.process_lock.release()
