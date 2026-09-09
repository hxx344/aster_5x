"""Independent account workers and shared market polling for the Linux service."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import logging
import os
import re
import threading
import time

import monitor
from .exchange import ExchangeError, LiveBroker, MarketData, credentials_for
from .execution import Executor
from .lock import ProcessLock
from .models import SYMBOLS, TIERS, TradingError, dec, next_leverage, plan_pair, positive, wire
from .paper import DemoMarket, PaperBroker
from .store import dumps

LOG = logging.getLogger("aster.trading")
DEFAULT_POLICY = {"symbols": list(SYMBOLS), "threshold": "10000", "order_notional": "1000", "margin_limit": "0.5", "spread_limit": "0.0005"}


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
    if set(policy) != set(DEFAULT_POLICY):
        raise TradingError("策略配置字段不完整")
    if not isinstance(policy["symbols"], list) or not policy["symbols"] or any(s not in SYMBOLS for s in policy["symbols"]) or len(set(policy["symbols"])) != len(policy["symbols"]):
        raise TradingError("交易市场配置无效")
    if positive(policy["threshold"], True) > 1000000000 or not 1 <= positive(policy["order_notional"]) <= 1000000:
        raise TradingError("阈值或单笔金额超出配置范围")
    if not 0 < positive(policy["margin_limit"]) <= dec("0.5") or not 0 < positive(policy["spread_limit"]) <= dec("0.0005"):
        raise TradingError("风险上限不得超过 50%，价差上限不得超过万 5")
    return account


def snapshot_json(snapshot):
    result = asdict(snapshot)
    result.pop("brackets", None)
    result.pop("fees", None)
    result["ratio"] = snapshot.ratio if snapshot.equity > 0 else None
    return json.loads(dumps(result))


class Engine:
    def __init__(self, store, demo=False, market=None):
        self.store, self.demo = store, demo
        self.market = market or (DemoMarket() if demo else MarketData())
        self.shutdown = threading.Event()
        self.thread = None
        self.process_lock = ProcessLock(store.path.with_suffix(".lock"))
        self.lock = threading.RLock()
        self.account_locks = {}
        self.brokers, self.signers, self.users = {}, {}, {}
        self.markets, self.views, self.rotation = {}, {}, {}
        self.ready = False
        self.error = "正在连接行情服务"
        self.notification_error = None
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

    def poll_market(self, symbol):
        try:
            with self.lock:
                tiers = set(TIERS)
                for view in self.views.values():
                    tiers.update(p["leverage"] for p in view.get("snapshot", {}).get("positions", []) if p["symbol"] == symbol)
            capacities = self.market.capacities(symbol, tiers)
            book = self.market.book(symbol)
            row = {"status": "ok", "capacities": {str(k): wire(v) for k, v in capacities.items()},
                   "checked_at": time.time(), "book": {**asdict(book), "spread": book.spread}}
            with self.lock:
                self.markets[symbol] = json.loads(dumps(row))
            return 5
        except (TradingError, KeyError, ValueError, TypeError) as exc:
            with self.lock:
                previous = self.markets.get(symbol, {})
                self.markets[symbol] = {**previous, "status": "error", "error": str(exc) if isinstance(exc, TradingError) else "行情数据格式异常"}
            return max(10, getattr(exc, "retry_after", 0))

    def capacities(self, symbol, leverage):
        with self.lock:
            row = self.markets.get(symbol, {}).copy()
        if row.get("status") != "ok" or time.time() - row.get("checked_at", 0) > 8:
            raise TradingError("市场额度快照过期或查询失败")
        capacities = {int(k): dec(v) for k, v in row["capacities"].items()}
        if leverage not in capacities:
            raise TradingError(f"等待 {leverage}x 额度数据")
        return capacities

    def tick_account(self, account_id):
        with self.account_lock(account_id):
            account = self.store.account(account_id)
            if not account:
                return 5
            try:
                broker = self.broker(account)
                snapshot = broker.snapshot(account["policy"]["symbols"])
                self.view(account_id, snapshot=snapshot_json(snapshot), credential_ready=True,
                          status="running" if account["enabled"] else "paused", reason="策略运行中" if account["enabled"] else "策略已暂停")
                executor = Executor(self.store, broker, self.market)
                pending = self.store.intent(account_id)
                if pending:
                    if self.live_allowed(account):
                        reason = executor.reconcile(account, pending)
                        current = self.store.intent(account_id)
                        status = "attention" if current and current["status"] == "attention" else "reconciling"
                    else:
                        status, reason = "attention", "服务器未启用实盘执行，保留未完成批次等待核对"
                    self.view(account_id, status=status, reason=reason)
                    self.strategy(account_id, pending["symbol"], reason, status)
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
                markets = policy["symbols"]
                start = self.rotation.get(account_id, 0) % len(markets)
                ordered = markets[start:] + markets[:start]
                last_reason = "等待交易条件"
                for symbol in ordered:
                    try:
                        long, short = snapshot.require_ready(symbol)
                        if self.market.rules[symbol].margin_asset != "USD1":
                            raise TradingError("仅允许 USD1 保证金市场")
                        capacities = self.capacities(symbol, long.leverage)
                        book = self.market.book(symbol)
                        if self.shutdown.is_set():
                            return 5
                        flat = long.qty + short.qty == 0
                        target = None
                        if flat and long.leverage != 4:
                            if capacities.get(4, dec(0)) > dec(policy["threshold"]) and book.spread <= dec(policy["spread_limit"]) and snapshot.ratio < dec(policy["margin_limit"]):
                                target = 4
                        elif not flat and long.qty == short.qty:
                            candidate = next((v for v in TIERS if v > long.leverage), None)
                            if candidate in capacities:
                                target = next_leverage(snapshot, symbol, capacities, book.mark)
                        if target is not None:
                            reason = executor.leverage(account, symbol, long.leverage, target)
                            self.strategy(account_id, symbol, reason, "leverage")
                            self.rotation[account_id] = (markets.index(symbol) + 1) % len(markets)
                            return 5
                        if flat and long.leverage != 4:
                            last_reason = "首次开仓等待 4x 额度条件"
                            self.strategy(account_id, symbol, last_reason)
                            continue
                        plan = plan_pair(snapshot, book, self.market.rules[symbol], capacities, policy)
                        self.strategy(account_id, symbol, plan.reason, projected_ratio=wire(plan.projected_ratio) if plan.projected_ratio is not None else None)
                        last_reason = plan.reason
                        if plan.qty:
                            reason = executor.open_pair(account, snapshot, symbol, plan, book)
                            # A completed pair is followed by an actual account risk check.
                            after = broker.snapshot(markets)
                            unresolved = self.store.intent(account_id)
                            phase = ("attention" if unresolved["status"] == "attention" else "reconciling") if unresolved else "filled"
                            self.view(account_id, snapshot=snapshot_json(after), reason=reason, status=phase if unresolved else "running")
                            self.strategy(account_id, symbol, reason, phase)
                            if after.ratio >= dec(policy["margin_limit"]):
                                account["enabled"] = False
                                self.store.save_account(account)
                                self.view(account_id, status="attention", reason="成交后保证金比率达到上限，已暂停新加仓")
                                self.store.event(account_id, "error", "成交后保证金比率达到上限，已暂停新加仓")
                                self.store.finish_campaign(account, "风险上限触发，暂停加仓", after.ratio)
                            self.rotation[account_id] = (markets.index(symbol) + 1) % len(markets)
                            return 5
                    except (TradingError, KeyError, ValueError, TypeError) as exc:
                        last_reason = str(exc) if isinstance(exc, TradingError) else "交易数据格式异常"
                        self.strategy(account_id, symbol, last_reason, "waiting")
                        if self.store.intent(account_id):
                            # No new symbol work until this account's intent is resolved.
                            raise
                        if isinstance(exc, ExchangeError) and exc.retry_after:
                            raise
                self.view(account_id, reason=last_reason)
                campaign = self.store.get("campaign:" + account_id)
                if campaign and (snapshot.ratio >= dec(policy["margin_limit"]) or time.time() - campaign["last_fill_at"] >= 60):
                    self.store.finish_campaign(account, last_reason, snapshot.ratio)
                return 5
            except (TradingError, KeyError, ValueError, TypeError) as exc:
                message = str(exc) if isinstance(exc, TradingError) else "账户响应格式异常，已停止本轮操作"
                with self.lock:
                    old_reason = self.views.get(account_id, {}).get("reason")
                if old_reason != message:
                    self.store.event(account_id, "error", message)
                self.view(account_id, status="error", reason=message, credential_ready=account_id in self.brokers)
                return max(10, getattr(exc, "retry_after", 0))

    def add_account(self, data):
        account = validate_account({**data, "enabled": False, "policy": {**DEFAULT_POLICY}})
        if self.demo and account["mode"] != "paper":
            raise TradingError("模拟环境只接受模拟账户")
        with self.lock:
            accounts = self.store.accounts()
            if len(accounts) >= 8:
                raise TradingError("单实例最多管理 8 个账户")
            if any(a["id"] == account["id"] or a["env_prefix"] == account["env_prefix"] for a in accounts):
                raise TradingError("账户标识或环境变量前缀已使用")
            self.store.save_account(account)
        self.store.event(account["id"], "config", "账户已添加，默认暂停")
        return account

    def configure(self, account_id, changes):
        with self.account_lock(account_id):
            account = self.store.account(account_id)
            if not account:
                raise TradingError("账户不存在")
            if account["enabled"] or self.store.intent(account_id):
                raise TradingError("请先暂停策略并等待当前批次完成")
            account["policy"] = {**account["policy"], **changes}
            validate_account(account)
            self.store.save_account(account)
            self.store.event(account_id, "config", "分批设置已更新")

    def enable(self, account_id, enabled):
        with self.account_lock(account_id):
            account = self.store.account(account_id)
            if not account:
                raise TradingError("账户不存在")
            if enabled:
                if not self.live_allowed(account):
                    raise TradingError("服务器尚未启用实盘执行（ASTER_ALLOW_LIVE=1）")
                if self.store.intent(account_id):
                    raise TradingError("请先核对未完成批次")
                snapshot = self.broker(account).snapshot(account["policy"]["symbols"])
                for symbol in account["policy"]["symbols"]:
                    snapshot.require_ready(symbol)
                if snapshot.ratio >= dec(account["policy"]["margin_limit"]):
                    raise TradingError("保证金比率已达到上限")
            account["enabled"] = enabled
            self.store.save_account(account)
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
            self.store.event(account_id, "control", "重新核对未完成批次；不重复提交原开仓订单")

    def notification_config(self):
        webhook = os.environ.get("FEISHU_WEBHOOK_URL", "")
        if not webhook:
            return None
        base = json.loads((monitor.ROOT / "config.json").read_text(encoding="utf-8"))
        return monitor.validate_config({**base, "feishu_enabled": True, "feishu_webhook": webhook,
                                       "feishu_sign_secret": os.environ.get("FEISHU_SIGN_SECRET", "")})

    def notify(self):
        if self.demo:
            return 5
        try:
            config = self.notification_config()
            if not config:
                return 5
            for item in self.store.due_notifications():
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
        with self.lock:
            accounts = [{**a, **self.views.get(a["id"], {"status": "starting", "reason": "等待读取账户", "credential_ready": False, "strategies": {}})} for a in self.store.accounts()]
            return json.loads(dumps({"demo": self.demo, "ready": self.ready, "error": self.error,
                "accounts": accounts, "markets": self.markets, "events": self.store.events(), "updated_at": time.time(),
                "notification": {"configured": bool(os.environ.get("FEISHU_WEBHOOK_URL")), "pending": self.store.pending_notifications(), "error": self.notification_error}}))

    def run(self):
        pending, due = {}, {}
        with ThreadPoolExecutor(max_workers=8, thread_name_prefix="aster") as pool:
            try:
                while not self.shutdown.is_set():
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
                            due[key] = time.monotonic() + delay
                            del pending[key]
                    jobs = {"market:" + s: (self.poll_market, s) for s in SYMBOLS}
                    jobs.update({"account:" + a["id"]: (self.tick_account, a["id"]) for a in self.store.accounts()})
                    jobs["notify"] = (self.notify,)
                    for key, (function, *args) in jobs.items():
                        if key not in pending and time.monotonic() >= due.get(key, 0):
                            pending[key] = pool.submit(function, *args)
                    self.shutdown.wait(.1)
            finally:
                self.shutdown.set()
        for broker in self.brokers.values():
            broker.close()
        if isinstance(self.market, MarketData):
            self.market.api.close()

    def start(self):
        self.process_lock.acquire()
        self.thread = threading.Thread(target=self.run, name="aster-engine", daemon=True)
        self.thread.start()

    def stop(self):
        self.shutdown.set()
        if self.thread:
            self.thread.join(timeout=90)
        if not self.thread or not self.thread.is_alive():
            self.process_lock.release()
