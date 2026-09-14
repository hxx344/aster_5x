"""Durable, isolated opening/holding/closing cycles with reduce-only repairs."""
import copy
import math
import time
import uuid
from contextlib import nullcontext
from fractions import Fraction
from time import monotonic

from .cycle import DailyVolumeLimitError, RollingVolumeLimitError
from .cycle_diagnostics import diagnostic_error, diagnostic_number
from .cycle_quality import (ObservedBroker, actual, clock_tick, estimate, estimate_from_plan,
                            new_quality, record_duration, timestamp)
from .exchange import ExchangeError, LeverageRejected, LiveBroker, RequestNotSent
from .execution import Executor, TERMINAL
from .models import AccountModeError, TradingError, cycle_margin_limit, dec, floor_step, positive, wire
from .paper import PaperBroker, PaperOrderAbsent


SIDES = ("LONG", "SHORT")


class _GuardedCycleBroker:
    """Fail a revoked local admission inside the known-not-sent boundary."""
    def __init__(self, broker, validate):
        self.broker, self.validate = broker, validate

    def __getattr__(self, name):
        return getattr(self.broker, name)

    def submit(self, orders):
        try:
            if self.validate is None:
                raise TradingError("循环账户热快照尚未授权，等待后台更新")
            self.validate()
        except Exception as exc:
            raise RequestNotSent(str(exc)) from exc
        return self.broker.submit(orders)


class CycleExecutor(Executor):
    def prepare_snapshot(self, account, symbol=None):
        """Admit one background live snapshot, or read the local paper ledger."""
        self._cycle_admission = None
        self._cycle_submit_guard = None
        account_id = account["id"]
        symbol = account["cycle"]["symbol"] if symbol is None else symbol
        if isinstance(self.broker, LiveBroker):
            lease = self.broker.cycle_hot_snapshot([symbol])
            snapshot, started, validate = lease.snapshot, lease.started_monotonic, lease.require_fresh
            validate()
            snapshot.require_fresh()
        else:
            started = monotonic()
            snapshot = self.broker.cycle_snapshot([symbol], fresh_modes=True)
            validate = None
        # Keep the authority local to this executor. Neither a copied snapshot
        # nor later changes to its wall-clock timestamp can renew admission.
        self._cycle_admission = (snapshot, account_id, symbol, started, validate)
        return snapshot

    @staticmethod
    def _require_admission_fresh(admission):
        started, now = admission[3], monotonic()
        if type(started) not in (int, float) or type(now) not in (int, float) \
                or not math.isfinite(started) or not math.isfinite(now) or not 0 <= now - started <= 8:
            raise TradingError("循环本轮账户快照已过期，等待重新核对")
        if admission[4] is not None:
            admission[4]()
            admission[0].require_fresh()

    def _observe_quality(self, intent):
        if not intent or intent.get("kind") != "cycle":
            return
        try:
            quality = intent.get("execution_quality")
            if not isinstance(quality, dict) or quality.get("version") != 1:
                # Old durable batches have no recoverable clock or quote sample.
                quality = new_quality(intent)
            quality.update(actual=actual(intent), updated_at=time.time())
            intent["execution_quality"] = quality
            self.store.record_cycle_execution_quality(intent)
            if self.last_completed_intent and self.last_completed_intent.get("id") == intent["id"]:
                self.last_completed_intent["execution_quality"] = copy.deepcopy(quality)
        except Exception:
            # Telemetry must never mask a trading/reconciliation exception or
            # strand exposure because a display write failed.
            pass

    def send(self, intent, orders, *, repair=False):
        self._cycle_admission = None
        guard = getattr(self, "_cycle_submit_guard", None)
        self._cycle_submit_guard = None
        if repair or intent.get("kind") != "cycle":
            return super().send(intent, orders, repair=repair)
        broker = self.broker
        if isinstance(broker, LiveBroker):
            validate = guard[1] if guard and guard[0] == intent["id"] else None
            broker = _GuardedCycleBroker(broker, validate)
        observed = None
        try:
            quality = intent.get("execution_quality")
            if not isinstance(quality, dict) or quality.get("version") != 1:
                quality = intent["execution_quality"] = new_quality(intent)
            clocks = getattr(self, "_quality_clocks", None)
            trigger_ticks, final_ticks = clocks[1:] if clocks and clocks[0] == intent["id"] else (None, None)
            observed = ObservedBroker(broker, quality, trigger_ticks, final_ticks)
        except Exception:
            pass
        try:
            # Reuse the original one-call send, validation and persistence logic.
            # A failed observation setup must still use the live admission guard.
            return Executor(self.store, observed or broker, self.market).send(intent, orders, repair=repair)
        finally:
            self._observe_quality(intent)

    def _record_volume_fills(self, intent, fills):
        # A single order may span more than the ledger's per-write row limit.
        # Commit bounded chunks; stable trade IDs make an interrupted retry safe.
        for offset in range(0, len(fills), 1000):
            self.store.record_cycle_fills(intent, fills[offset:offset + 1000])

    def _require_daily_room(self, account, plan, config):
        if plan.phase != "open":
            return
        now = time.time()
        if self.store.cycle_volume_backlog(account["id"], limit=1, since=max(0, now - 86400)):
            raise TradingError("循环成交明细尚未补齐，等待核对后再开新仓")
        # Even unlimited accounts read the ledger: missing accounting cannot be
        # silently treated as zero when the limit is subsequently configured.
        # Both windows share one timestamp, including at a UTC day boundary.
        daily = self.store.cycle_daily_volume(account["id"], now=now)
        rolling = self.store.cycle_rolling_volume(account["id"], now=now)
        used_day = Fraction(positive(daily["volume"], True))
        used_24h = Fraction(positive(rolling["volume"], True))
        limit = Fraction(positive(config.get("daily_volume_limit", "0"), True))
        roundtrip = 2 * (Fraction(positive(plan.long_notional)) + Fraction(positive(plan.short_notional)))
        def exceeded(code, title, error_type):
            checks = [{"code": check_code, "label": label, "actual": diagnostic_number(used + roundtrip),
                       "required": "≤ " + diagnostic_number(limit), "unit": "USD1",
                       "passed": used + roundtrip <= limit}
                      for check_code, label, used in (("daily_projected_volume", "UTC 日预计累计成交量", used_day),
                                                      ("rolling_projected_volume", "近 24 小时预计累计成交量", used_24h))]
            context = [{"label": label, "value": diagnostic_number(value), "unit": "USD1"}
                       for label, value in (("本轮预计开平交易量", roundtrip),
                                            ("UTC 日已用成交量", used_day),
                                            ("UTC 日剩余额度", max(Fraction(0), limit - used_day)),
                                            ("近 24 小时已用成交量", used_24h),
                                            ("近 24 小时剩余额度", max(Fraction(0), limit - used_24h)),
                                            ("成交量上限", limit))]
            return diagnostic_error(code, title, symbol=plan.symbol, phase="open", checked_at=now,
                                    checks=checks, context=context, error_type=error_type)
        if limit and used_day + roundtrip > limit and used_day >= used_24h:
            raise exceeded("execution_daily_volume",
                           "今日 UTC 交易量余量不足以覆盖本轮预计开仓及平仓，待 UTC 日与滚动 24 小时额度均满足后自动恢复",
                           DailyVolumeLimitError)
        if limit and used_24h + roundtrip > limit:
            raise exceeded("execution_rolling_volume",
                           "最近 24 小时交易量余量不足以覆盖本轮预计开仓及平仓，待较早成交移出窗口且两项额度均满足后自动恢复",
                           RollingVolumeLimitError)

    def sync_volume(self, account, intent):
        """Idempotently account known fills; missing details never block reductions."""
        if intent.get("kind") != "cycle":
            return True
        if intent.get("account_id") != account["id"]:
            intent["volume_error"] = "循环成交补账的账户不一致"
            return False
        original = {key: copy.deepcopy(intent.get(key)) for key in ("volume_queries", "volume_receipts")}
        def save_state():
            synced = self.store.save_cycle_volume_state(intent, original)
            if synced:
                intent.pop("volume_error", None)
                intent["volume_synced"] = True
            return synced
        errors = []
        orders = intent.get("orders", []) + intent.get("repairs", [])
        with getattr(self.broker, "cycle_volume_budget", nullcontext)():
            for order in orders:
                cid = order["newClientOrderId"]
                receipt = intent.get("receipts", {}).get(cid)
                if receipt is None:
                    errors.append("循环订单回执仍待核对")
                    continue
                checkpoint = intent.setdefault("volume_queries", {}).setdefault(cid, {})
                try:
                    self.validate_receipt(order, receipt)
                    if isinstance(self.broker, LiveBroker) and (receipt.get("orderId") is None or receipt["status"] not in TERMINAL) \
                            and dec(receipt["executedQty"]):
                        fresh = self.broker.query(order["symbol"], cid)
                        self.validate_receipt(order, fresh)
                        if dec(fresh["executedQty"]) < dec(receipt["executedQty"]) or (
                                receipt["status"] in TERMINAL and dec(fresh["executedQty"]) != dec(receipt["executedQty"])):
                            raise TradingError("循环订单查询成交数量与原回执冲突")
                        receipt = intent["receipts"][cid] = fresh
                    signature = [str(receipt.get("orderId", "")), wire(dec(receipt["executedQty"])), receipt["status"]]
                    if not dec(receipt["executedQty"]):
                        if receipt["status"] not in TERMINAL:
                            errors.append("循环订单尚未终结，成交明细仍待核对")
                        continue
                    if intent.setdefault("volume_receipts", {}).get(cid) == signature:
                        if receipt["status"] not in TERMINAL:
                            errors.append("循环订单尚未终结，继续核对新增成交")
                        continue
                    created_at = intent.get("order_times", {}).get(cid, intent["created_at"])
                    fills = self.broker.cycle_trades(order, receipt, created_at, checkpoint=checkpoint)
                    # A historical paper adapter may attach its stable synthetic
                    # orderId here; the ledger validates against the durable copy.
                    if save_state():
                        return True
                    self._record_volume_fills(intent, fills)
                    signature = [str(receipt.get("orderId", "")), wire(dec(receipt["executedQty"])), receipt["status"]]
                    intent["volume_receipts"][cid] = signature
                    if receipt["status"] not in TERMINAL:
                        errors.append("循环订单尚未终结，继续核对新增成交")
                except Exception as exc:
                    # Valid matching fills from an incomplete page are useful
                    # accounting evidence even before the whole order is visible.
                    try:
                        if save_state():
                            return True
                        partial = list(checkpoint.get("fills", {}).values())
                        if partial:
                            self._record_volume_fills(intent, partial)
                    except Exception as write_error:
                        errors.append(str(write_error))
                    errors.append(str(exc))
            try:
                if errors:
                    intent["volume_error"] = "；".join(dict.fromkeys(errors))[:1000]
                    return save_state()
                intent.pop("volume_error", None)
                if save_state():
                    return True
                if intent.get("status") in ("complete", "aborted"):
                    self.store.mark_cycle_volume_synced(intent["id"])
                    intent["volume_synced"] = True
                return True
            except Exception as exc:
                intent["volume_error"] = str(exc)[:1000]
                try:
                    if save_state():
                        return True
                except Exception:
                    pass
                # Accounting failure is surfaced to the scheduler; never turn it
                # into an exception that can strand a partially closed position.
                return False

    def attention(self, account, intent, reason):
        result = super().attention(account, intent, reason)
        self.sync_volume(account, intent)
        return result

    def _require_progress(self, account, progress):
        saved = self.store.get("cycle:" + account["id"])
        if not isinstance(progress, dict) or not progress.get("run_id") or not saved or saved.get("run_id") != progress["run_id"]:
            raise TradingError("独立循环进度与持久记录不一致，等待核对")

    @staticmethod
    def _ready(snapshot, symbol):
        # Closing must remain possible even if equity has become nonpositive.
        snapshot.require_fresh()
        snapshot.require_modes([symbol])
        if not snapshot.can_trade:
            raise TradingError("账户没有交易权限")
        return snapshot.pair(symbol)

    def start(self, account, snapshot, plan, progress, before_submit=None, *, trigger=None):
        admission = getattr(self, "_cycle_admission", None)
        self._cycle_admission = None
        self._cycle_submit_guard = None
        self.last_snapshot = self.last_completed_intent = None
        if self.store.intent(account["id"]):
            raise TradingError("已有批次正在执行")
        self._require_progress(account, progress)
        saved_account = self.store.account(account["id"])
        if not account.get("enabled") or not account.get("cycle", {}).get("enabled") or not saved_account \
                or not saved_account.get("enabled") or not saved_account.get("cycle", {}).get("enabled"):
            raise TradingError("独立循环或账户策略未启动")
        if plan.phase not in ("open", "close"):
            raise TradingError("独立循环批次阶段无效")
        symbol, qty = plan.symbol, positive(plan.qty)
        quality, trigger_ticks, final_ticks = None, None, None
        try:
            quality = new_quality({"symbol": symbol, "phase": plan.phase, "quantity": wire(qty), "orders": []}, trigger)
            trigger_ticks = timestamp(trigger.get("received_monotonic")) if isinstance(trigger, dict) else None
        except Exception:
            pass
        config = progress.get("config") or account["cycle"]
        self._require_daily_room(account, plan, config)
        if symbol != config["symbol"] or plan.leverage != config["leverage"]:
            raise TradingError("独立循环计划与本轮配置不一致")
        rule = self.market.rules[symbol]
        if qty != floor_step(qty, rule.step) or not rule.min_qty <= qty <= rule.max_qty:
            raise TradingError("循环批次数量不符合交易规则")
        final_account_started = clock_tick()
        prepared = admission is not None and admission[0] is snapshot \
            and admission[1] == account["id"] and admission[2] == symbol
        if prepared:
            self._require_admission_fresh(admission)
            long, short = self._ready(snapshot, symbol)
        else:
            # Direct callers must compare a newly admitted live cache version;
            # only the local paper broker retains the old ledger-read fallback.
            selected = self._ready(snapshot, symbol)
            if isinstance(self.broker, LiveBroker):
                snapshot = self.prepare_snapshot(account, symbol)
                admission, self._cycle_admission = self._cycle_admission, None
                prepared = True
                self._require_admission_fresh(admission)
            else:
                snapshot = self.broker.cycle_snapshot([symbol], fresh_modes=True)
            long, short = self._ready(snapshot, symbol)
            if any(current.qty != previous.qty or current.leverage != previous.leverage
                   for current, previous in zip((long, short), selected)):
                raise TradingError("独立循环规划后的仓位或杠杆已变化，等待重新核对")
        record_duration(quality, "final_account_ms", final_account_started)
        if long.leverage != plan.leverage:
            raise TradingError("独立循环实际杠杆与设定不一致")
        baseline = {"LONG": wire(long.qty), "SHORT": wire(short.qty)}
        if plan.phase == "open":
            if progress.get("phase", "waiting_open") != "waiting_open" or long.qty or short.qty:
                raise TradingError("独立循环开仓前必须确认所选品种空仓")
            if any(dec(progress.get("quantities", {}).get(side, "0")) for side in SIDES):
                raise TradingError("独立循环仍有未核对的本轮持仓")
            snapshot.ratio
        else:
            if progress.get("phase") not in ("holding", "waiting_close"):
                raise TradingError("独立循环当前阶段不能平仓")
            if any(dec(progress.get("quantities", {}).get(side, "0")) != dec(baseline[side])
                   or dec(baseline[side]) != qty for side in SIDES):
                raise TradingError("独立循环实际持仓与本轮记录不一致，禁止处理外部仓位")
            opened_at = progress.get("opened_at")
            if type(opened_at) not in (int, float) or not math.isfinite(opened_at) or time.time() - opened_at < config["hold_seconds"]:
                raise TradingError("独立循环尚未达到本轮最短持仓时间")
        book = self.market.cycle_book(symbol) if isinstance(self.broker, LiveBroker) else self.market.book(symbol)
        book.require_fresh()
        final_observation = None
        if before_submit is not None:
            final_check_started = clock_tick()
            final_observation = before_submit(snapshot)
            record_duration(quality, "final_check_ms", final_check_started)
        try:
            if isinstance(final_observation, dict):
                depth = final_observation.get("depth")
                checked_at = timestamp(final_observation.get("checked_at"))
                final_ticks = timestamp(final_observation.get("checked_monotonic"))
            else:
                # A cache read only: never invoke market.depth's REST fallback.
                stream = getattr(self.market, "depth_stream", None)
                depth = stream.snapshot(symbol) if stream is not None else None
                checked_at = time.time() if depth is not None else None
            if quality is not None:
                quality_plan = final_observation.get("quality_plan") if isinstance(final_observation, dict) else None
                quality["final_estimate"] = (
                    estimate_from_plan(wire(qty), quality_plan, depth, checked_at, symbol=symbol, phase=plan.phase)
                    if quality_plan is not None else estimate(wire(qty), depth, checked_at))
                if quality["final_estimate"]["status"] != "available":
                    final_ticks = None
                elif not isinstance(final_observation, dict):
                    final_ticks = time.monotonic()
        except Exception:
            final_ticks = None
        if prepared:
            self._require_admission_fresh(admission)
        self._ready(snapshot, symbol)
        latest = self.store.account(account["id"])
        if not latest.get("enabled") or not latest.get("cycle", {}).get("enabled"):
            raise TradingError("独立循环或账户策略已暂停")
        # Re-read UTC-day usage after the last account/quote callback. This also
        # handles a midnight boundary during the admission reads.
        self._require_daily_room(latest, plan, latest["cycle"])
        book.require_fresh()
        token = uuid.uuid4().hex
        orders = [self.order(symbol, side,
                            ("BUY" if side == "LONG" else "SELL") if plan.phase == "open"
                            else ("SELL" if side == "LONG" else "BUY"), qty,
                            "C" + side[0] + token[:27]) for side in SIDES]
        intent = {"id": token, "kind": "cycle", "account_id": account["id"], "symbol": symbol,
                  "run_id": progress["run_id"],
                  "phase": plan.phase, "leverage": plan.leverage, "quantity": wire(qty),
                  "status": "pending", "created_at": time.time(), "baseline": baseline,
                  "progress": copy.deepcopy(progress), "orders": orders, "receipts": {},
                  "repairs": [], "repair_attempts": 0,
                  "order_times": {order["newClientOrderId"]: time.time() for order in orders}}
        intent["progress"]["config"] = copy.deepcopy(config)
        if quality is not None:
            quality.update(intent_id=token, created_at=intent["created_at"])
            intent["execution_quality"] = quality
        action = "开仓" if plan.phase == "open" else "平仓"
        persist_started = clock_tick()
        if isinstance(self.broker, LiveBroker):
            self._cycle_submit_guard = (token, lambda: self._require_admission_fresh(admission))
        try:
            self.store.create_cycle_intent(intent, f"{symbol} 独立循环同时{action}多空，每边 {wire(qty)}，{plan.leverage}x")
            record_duration(quality, "persist_ms", persist_started)
            self._quality_clocks = (token, trigger_ticks, final_ticks)
            self.send(intent, orders)
        finally:
            self._quality_clocks = None
            self._cycle_submit_guard = None
        return self.reconcile(account, intent)

    def set_leverage(self, account, snapshot, progress, before_submit=None):
        self._cycle_admission = None
        self._cycle_submit_guard = None
        self.last_snapshot = self.last_completed_intent = None
        if self.store.intent(account["id"]):
            raise TradingError("已有批次正在执行")
        self._require_progress(account, progress)
        saved_account = self.store.account(account["id"])
        if not account.get("enabled") or not account.get("cycle", {}).get("enabled") or not saved_account \
                or not saved_account.get("enabled") or not saved_account.get("cycle", {}).get("enabled"):
            raise TradingError("独立循环或账户策略未启动")
        config = progress.get("config") or account["cycle"]
        symbol, target = config["symbol"], config["leverage"]
        if type(target) is not int or not 1 <= target <= 125:
            raise TradingError("独立循环杠杆必须为 1 至 125 的整数")
        long, short = self._ready(snapshot, symbol)
        if long.qty or short.qty or any(dec(progress.get("quantities", {}).get(side, "0")) for side in SIDES):
            raise TradingError("独立循环只在所选品种确认空仓时调整杠杆")
        if target == long.leverage:
            return f"独立循环当前已为 {target}x"
        intent = {"id": uuid.uuid4().hex, "kind": "cycle_leverage", "account_id": account["id"],
                  "run_id": progress["run_id"],
                  "symbol": symbol, "previous": long.leverage, "target": target,
                  "status": "pending", "created_at": time.time(), "progress": copy.deepcopy(progress)}
        self.store.save_intent(intent)
        def check_leverage_submit(fresh):
            current = self.store.account(account["id"])
            if not current or not current.get("enabled") or not current.get("cycle", {}).get("enabled"):
                raise RequestNotSent("独立循环或账户策略已暂停")
            selected = self._ready(fresh, symbol)
            if any(position.qty for position in selected):
                raise RequestNotSent("独立循环杠杆提交前发现外部仓位")
            if before_submit is not None:
                before_submit(fresh)
            current = self.store.account(account["id"])
            if not current or not current.get("enabled") or not current.get("cycle", {}).get("enabled"):
                raise RequestNotSent("独立循环或账户策略已暂停")
        try:
            response = self.broker.set_cycle_leverage(symbol, target, checked_snapshot=snapshot, before_submit=check_leverage_submit)
            if not isinstance(response, dict) or response.get("symbol") != symbol or LiveBroker._leverage(response.get("leverage")) != target:
                raise TradingError("独立循环杠杆调整回执无效，等待实际账户确认")
            intent["change_response"] = {"symbol": symbol, "leverage": target}
        except (RequestNotSent, LeverageRejected) as exc:
            intent.update(status="aborted", last_error=str(exc))
            self.store.complete_cycle(intent, intent["progress"])
            raise
        except TradingError as exc:
            intent.update(last_error=str(exc), submission_error=str(exc))
        self.store.save_intent(intent)
        return f"正在核对独立循环 {symbol} {long.leverage}x→{target}x 杠杆调整结果"

    def reconcile(self, account, intent=None):
        self._cycle_admission = None
        self._cycle_submit_guard = None
        self.last_snapshot = self.last_completed_intent = None
        with getattr(self.broker, "reconciliation_budget", nullcontext)():
            intent = intent or self.store.intent(account["id"])
            try:
                return self._reconcile_cycle(account, intent)
            finally:
                self._observe_quality(intent)

    def _reconcile_cycle(self, account, intent=None):
        intent = intent or self.store.intent(account["id"])
        if not intent:
            return "没有未完成循环批次"
        if intent.get("status") in ("complete", "aborted"):
            self.sync_volume(account, intent)
            return "独立循环批次已结束"
        if intent["kind"] == "cycle_leverage":
            return self._reconcile_leverage(account, intent)
        if intent["kind"] != "cycle":
            raise TradingError("独立循环不能处理其他策略批次")
        if isinstance(self.broker, PaperBroker):
            self.broker.reload()
        orders = intent["orders"] + intent["repairs"]
        unresolved = []
        for order in orders:
            cid = order["newClientOrderId"]
            known = intent["receipts"].get(cid)
            if known and known.get("status") in TERMINAL:
                try:
                    self.validate_receipt(order, known)
                except TradingError as exc:
                    return self.attention(account, intent, str(exc))
                continue
            try:
                row = self.broker.query(intent["symbol"], cid)
                self.validate_receipt(order, row)
                intent["receipts"][cid] = row
                if row["status"] not in TERMINAL:
                    unresolved.append(cid)
                    if time.time() - intent["created_at"] > 10:
                        self.broker.cancel(intent["symbol"], cid)
            except ExchangeError as exc:
                if isinstance(self.broker, PaperBroker) and isinstance(exc, PaperOrderAbsent):
                    intent["receipts"][cid] = {**order, "clientOrderId": cid, "status": "REJECTED",
                                               "executedQty": "0", "avgPrice": "0", "paper_not_committed": True}
                else:
                    unresolved.append(cid)
                    intent["last_error"] = str(exc)
            except TradingError as exc:
                return self.attention(account, intent, str(exc))
        # A simulated repair batch only refunds its bounded attempt if no leg
        # committed. Keep the refund and known-absent receipts in one write.
        absent_repair = False
        for batch, ids in intent.get("repair_batches", {}).items():
            if batch not in intent.setdefault("absent_repair_batches", []) and all(
                    intent["receipts"].get(cid, {}).get("paper_not_committed") for cid in ids):
                intent["absent_repair_batches"].append(batch)
                intent["repair_attempts"] = max(0, intent["repair_attempts"] - 1)
                absent_repair = True
        self.store.save_intent(intent)
        if unresolved:
            self.sync_volume(account, intent)
            if time.time() - intent["created_at"] > 120:
                return self.attention(account, intent, "独立循环订单结果仍不确定，已暂停；请核对未完成批次")
            return "核对独立循环订单回执中，不重复提交"
        snapshot = self.broker.cycle_snapshot([intent["symbol"]])
        snapshot.require_fresh()
        try:
            snapshot.require_modes([intent["symbol"]])
        except AccountModeError as exc:
            return self.attention(account, intent, str(exc))
        long, short = snapshot.pair(intent["symbol"])
        if long.leverage != intent["leverage"]:
            return self.attention(account, intent, "独立循环实际杠杆被改变，已暂停并等待核对")
        original = {side: Fraction(0) for side in SIDES}
        repaired = {side: Fraction(0) for side in SIDES}
        for order in intent["orders"]:
            original[order["positionSide"]] += Fraction(dec(intent["receipts"][order["newClientOrderId"]]["executedQty"]))
        for order in intent["repairs"]:
            repaired[order["positionSide"]] += Fraction(dec(intent["receipts"][order["newClientOrderId"]]["executedQty"]))
        opening = intent["phase"] == "open"
        expected = {side: Fraction(dec(intent["baseline"][side])) + (original[side] if opening else -original[side]) - repaired[side]
                    for side in SIDES}
        actual = {"LONG": Fraction(long.qty), "SHORT": Fraction(short.qty)}
        if any(expected[side] < 0 or actual[side] != expected[side] for side in SIDES):
            return self.attention(account, intent, "独立循环持仓变化与回执不一致，禁止处理外部仓位；已暂停并等待核对")
        full_open = opening and not intent["repairs"] and all(original[side] == Fraction(dec(intent["quantity"])) for side in SIDES)
        if full_open:
            config = intent["progress"]["config"]
            minimum, maximum = Fraction(dec(config["min_notional"])), Fraction(dec(config["max_notional"]))
            notionals = {side: Fraction(0) for side in SIDES}
            for order in intent["orders"]:
                receipt = intent["receipts"][order["newClientOrderId"]]
                notionals[order["positionSide"]] += Fraction(dec(receipt["executedQty"])) * Fraction(dec(receipt["avgPrice"]))
            compared = list(notionals.values()) if config["notional_scope"] == "per_side" else [sum(notionals.values())]
            if any(not minimum <= amount <= maximum for amount in compared):
                full_open = False
                scope = "每边" if config["notional_scope"] == "per_side" else "多空合计"
                intent["rollback_reason"] = ("实际成交金额超出本轮范围：多头 " + diagnostic_number(notionals["LONG"])
                                             + " USD1，空头 " + diagnostic_number(notionals["SHORT"])
                                             + " USD1，多空合计 " + diagnostic_number(sum(notionals.values()))
                                             + " USD1；要求" + scope + "金额 " + diagnostic_number(minimum)
                                             + "–" + diagnostic_number(maximum) + " USD1")
            try:
                margin_limit = cycle_margin_limit(account["policy"])
                if snapshot.margin_exceeds(margin_limit):
                    full_open = False
                    occupied, equity = snapshot.occupied_margin_exact, Fraction(snapshot.equity)
                    intent["rollback_reason"] = ("成交后实际保证金占用超过账户上限：当前 "
                                                 + diagnostic_number(occupied / equity, 100) + "%，要求 ≤ "
                                                 + diagnostic_number(margin_limit, 100) + "%（基础上限 + 5 个百分点，最高 100%）；"
                                                 + "账户总占用 " + diagnostic_number(occupied) + " USD1，总权益 "
                                                 + diagnostic_number(equity) + " USD1")
            except TradingError:
                full_open = False
                intent["rollback_reason"] = "成交后无法确认有效的保证金占用"
        if full_open:
            try:
                self._ready(snapshot, intent["symbol"])
            except TradingError as exc:
                return self.attention(account, intent, str(exc))
            progress = copy.deepcopy(intent["progress"])
            progress.update(phase="holding", quantities={side: wire(actual[side]) for side in SIDES}, opened_at=time.time(),
                            failure_count=0, retry_at=0, close_eligible_at=None)
            return self._finish(intent, progress, snapshot, "独立循环双向开仓已核实，开始计算持仓时间")
        if not any(actual.values()):
            progress = copy.deepcopy(intent["progress"])
            progress.update(phase="waiting_open", quantities=dict.fromkeys(SIDES, "0"), opened_at=None, close_eligible_at=None)
            if opening:
                failures = max(0, progress.get("failure_count", 0)) + 1
                delay = min(300, 30 * 2 ** min(failures - 1, 4))
                progress.update(failure_count=failures, retry_at=time.time() + delay)
            if not opening:
                progress["completed_cycles"] = progress.get("completed_cycles", 0) + 1
            intent["status"] = "aborted" if opening else "complete"
            return self._finish(intent, progress, snapshot,
                                f"独立循环{intent.get('rollback_reason', '开仓未完整成交')}，已核实空仓，{delay} 秒后重试" if opening
                                else "独立循环多空已全部平仓，本轮完成")
        # A partial opening is discarded entirely. Never buy/sell extra quantity
        # to repair an imbalance, and never touch a preexisting user position.
        try:
            self._ready(snapshot, intent["symbol"])
        except TradingError as exc:
            return self.attention(account, intent, str(exc))
        if absent_repair:
            return "模拟循环减仓未写入账本，稍后重新核对并重试"
        if intent["repair_attempts"] >= 3:
            return self.attention(account, intent, "独立循环减仓补偿尚未完成，已暂停；请核对剩余持仓")
        book = self.market.book(intent["symbol"])
        book.require_fresh()
        snapshot.require_fresh()
        rule = self.market.rules[intent["symbol"]]
        batch = uuid.uuid4().hex
        repairs = []
        for side in SIDES:
            if not actual[side]:
                continue
            qty = floor_step(min(actual[side], Fraction(rule.max_qty), Fraction(book.bid_qty if side == "LONG" else book.ask_qty)), rule.step)
            if qty < rule.min_qty:
                return self.attention(account, intent, "独立循环剩余数量或盘口不足以安全减仓，已暂停并等待核对")
            repair = self.order(intent["symbol"], side, "SELL" if side == "LONG" else "BUY", qty,
                                "CR" + side[0] + uuid.uuid4().hex[:26])
            repairs.append(repair)
        intent["repairs"].extend(repairs)
        intent.setdefault("order_times", {}).update({order["newClientOrderId"]: time.time() for order in repairs})
        intent.setdefault("repair_batches", {})[batch] = [order["newClientOrderId"] for order in repairs]
        intent["repair_attempts"] += 1
        intent["status"] = "repair"
        self.store.save_intent(intent)
        self.store.event(account["id"], "cycle", f"{intent['symbol']} 独立循环仅减仓补偿本轮剩余数量")
        not_sent = self.send(intent, repairs, repair=True)
        if not_sent is not None:
            raise not_sent
        return self._reconcile_cycle(account, intent)

    def _reconcile_leverage(self, account, intent):
        snapshot = self.broker.cycle_snapshot([intent["symbol"]], fresh_modes=True)
        try:
            long, short = self._ready(snapshot, intent["symbol"])
        except TradingError as exc:
            return self.attention(account, intent, str(exc))
        if long.qty or short.qty:
            return self.attention(account, intent, "独立循环杠杆核对期间发现外部仓位，已暂停")
        if long.leverage == intent["target"]:
            return self._finish(intent, intent["progress"], snapshot, f"独立循环实际杠杆已核实为 {long.leverage}x")
        if isinstance(self.broker, PaperBroker) and long.leverage == intent["previous"]:
            intent["status"] = "aborted"
            return self._finish(intent, intent["progress"], snapshot, "模拟循环杠杆调整未写入账本，等待重新检查")
        if time.time() - intent["created_at"] > 120:
            return self.attention(account, intent, f"独立循环杠杆调整结果仍不确定：目标 {intent['target']}x，实际 {long.leverage}x；已暂停且不重复提交")
        return f"等待账户确认独立循环 {intent['symbol']} 目标 {intent['target']}x，实际 {long.leverage}x"

    def _finish(self, intent, progress, snapshot, message):
        if intent.get("status") != "aborted":
            intent["status"] = "complete"
        progress = {**progress, "reason": message}
        self.store.complete_cycle(intent, progress)
        account = self.store.account(intent["account_id"])
        if intent.get("kind") == "cycle" and account and not self.sync_volume(account, intent):
            message += "；成交明细待补账，暂停下一轮开仓"
        self.last_snapshot, self.last_completed_intent = snapshot, copy.deepcopy(intent)
        return message
