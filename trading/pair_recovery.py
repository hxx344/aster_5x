"""Explicit review of unresolved ordinary opens; never infer fills from positions."""
from contextlib import ExitStack
from copy import deepcopy
from fractions import Fraction
import math
import re
import time
import uuid

from .exchange import AmbiguousOrder, ExchangeError, LiveBroker, api_wait_notice
from .exchange_messages import exchange_reason
from .execution import Executor, TERMINAL
from .models import TradingError, dec, positive, require_supported_leverage, wire
from .paper import PaperBroker, PaperOrderAbsent

SYMBOL = "XAUUSD1"
SIDES = (("long", "LONG"), ("short", "SHORT"))
MIN_AGE = 120
PREVIEW_TTL = 300
MAX_WATCH_BATCHES = 8


def _query(broker, order):
    try:
        row = broker.query(SYMBOL, order["newClientOrderId"])
    except PaperOrderAbsent:
        if isinstance(broker, PaperBroker):
            return None
        raise
    except ExchangeError as exc:
        if (not isinstance(exc, AmbiguousOrder) and exc.code == -2013 and not exc.retry_after
                and exc.http_status in (None, 400, 404)):
            return None
        raise
    Executor.validate_receipt(order, row)
    if positive(row.get("origQty")) != positive(order["quantity"]):
        raise TradingError("原订单回执的委托数量不匹配，不能采纳")
    return row


def require_archived_orders_clear(engine, pair, *, state=None, brokers=None):
    """Only new writes use this guard; reductions and existing recovery remain possible."""
    from .pair_execution import PairTrader
    state = engine.store.get("pair_runtime:" + pair["id"], {}) if state is None else state
    watch = state.get("recovery_watch")
    if watch is None:
        return
    batches = watch.get("batches") if isinstance(watch, dict) else None
    if not isinstance(batches, list) or not 1 <= len(batches) <= MAX_WATCH_BATCHES:
        raise TradingError("人工归档订单跟踪记录无效，禁止新开仓与划转")
    _, current_brokers, identities = PairTrader(engine)._members(pair)
    brokers = current_brokers if brokers is None else brokers
    for batch in batches:
        if not isinstance(batch, dict) or batch.get("identities") != identities:
            raise TradingError("人工归档订单的账户身份已变化，禁止新开仓与划转")
        legs = batch.get("legs")
        if (not isinstance(legs, list) or len(legs) != 2 or any(not isinstance(leg, dict) for leg in legs)
                or {leg.get("key") for leg in legs} != {"long", "short"}
                or any(not isinstance(leg.get("order"), dict) for leg in legs)):
            raise TradingError("人工归档订单明细无效，禁止新开仓与划转")
        for leg in legs:
            row = _query(brokers[leg["key"]], leg["order"])
            if row is not None and (row["status"] not in TERMINAL or dec(row["executedQty"]) != 0):
                raise TradingError("已人工归档的原订单出现成交或活动回执，停止新开仓与划转；请核对原订单和实际仓位")


class PairOrderRecovery:
    def __init__(self, manager):
        self.manager, self.engine, self.store = manager, manager.engine, manager.store
        self.previews = {}

    @staticmethod
    def _fingerprint(pair, state, margin, accounts):
        value = deepcopy(state)
        for key in ("updated_at", "reason", "phase", "attention", "retry_after", "snapshots", "margin"):
            value.pop(key, None)
        for leg in value["pending"]["legs"]:
            leg.pop("error", None)
        return {"pair": pair, "state": value, "margin": margin, "accounts": accounts}

    @staticmethod
    def _eligible(pair, state, margin, *, archive=True):
        from .pairing import has_cycle_quantity
        if pair.get("enabled") is not False:
            raise TradingError("请先暂停配对组，再核对遗留订单与持仓")
        if not isinstance(state, dict) or not isinstance(margin, dict):
            raise TradingError("配对或划转记录无效，不能人工归档")
        pending = state.get("pending")
        if (not isinstance(pending, dict) or pending.get("kind") != "ordinary" or pending.get("phase") != "open"
                or pending.get("symbol") != SYMBOL):
            raise TradingError("人工归档仅支持普通开仓未决批次；循环、减仓和杠杆调整须继续原恢复流程")
        progress = state.get("progress")
        if (not isinstance(progress, dict) or not isinstance(progress.get("quantities"), dict)
                or set(progress["quantities"]) != {"LONG", "SHORT"}):
            raise TradingError("循环新增仓位记录不完整，不能人工归档")
        if (not isinstance(pending.get("repairs"), list)
                or type(pending.get("repair_attempts")) is not int or not 0 <= pending["repair_attempts"] <= 3):
            raise TradingError("原批次补偿记录无效，无法核对")
        if ((archive and (pending["repairs"] or pending["repair_attempts"])) or has_cycle_quantity(state)
                or margin.get("pending") is not None
                or margin.get("status") in ("submitting", "acknowledged", "accepted", "unknown")):
            raise TradingError("仍有补偿减仓、循环新增仓位或未决划转，不能人工归档")
        created = pending.get("created_at")
        if type(created) not in (int, float) or not math.isfinite(created) or created <= 0:
            raise TradingError("原批次缺少有效创建时间，不能核对历史订单")
        age = time.time() - created
        if age < 0:
            raise TradingError("原批次创建时间晚于当前时间，无法核对")
        if archive and age < MIN_AGE:
            raise TradingError("原批次创建未满 120 秒，请等待自动查询后再核对")
        if archive and age + 60 >= 7 * 86400:
            raise TradingError("原批次超出完整历史核对窗口，不能使用此恢复入口")
        if not isinstance(pending.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", pending["id"]):
            raise TradingError("原批次编号无效")
        require_supported_leverage(pending.get("leverage"))
        qty = positive(pending.get("quantity"))
        if any(not isinstance(value, dict) or set(value) != {"LONG", "SHORT"}
               for value in (pending.get("before"), pending.get("target"), state.get("owned"))):
            raise TradingError("原批次基线、目标或底仓字段不完整，不能人工归档")
        legs = pending.get("legs")
        if (not isinstance(legs, list) or len(legs) != 2 or any(not isinstance(leg, dict) for leg in legs)
                or {leg.get("key") for leg in legs} != {"long", "short"}):
            raise TradingError("原批次需包含两个有效的指定方向订单")
        cids = set()
        for leg in legs:
            side = leg["key"].upper()
            label = "A · 只多" if side == "LONG" else "B · 只空"
            order = leg.get("order")
            if not isinstance(order, dict):
                raise TradingError(f"{label}：原委托记录缺失，无法核对")
            expected = {"symbol": SYMBOL, "positionSide": side,
                        "side": "BUY" if side == "LONG" else "SELL", "type": "MARKET"}
            for field, value in expected.items():
                if order.get(field) != value:
                    raise TradingError(f"{label}：原委托 {field} 字段不符合本批开仓记录，无法核对")
            if positive(order.get("quantity")) != qty:
                raise TradingError(f"{label}：原委托数量与批次数量不符，无法核对")
            if leg.get("dispatch") not in {"prepared", "sending"}:
                raise TradingError(f"{label}：原订单发送状态无效，无法核对")
            if leg.get("receipt") is not None:
                try:
                    Executor.validate_receipt(order, leg["receipt"])
                except TradingError as exc:
                    raise TradingError(f"{label}：已保存回执无效；{exchange_reason(str(exc))}") from None
                if archive:
                    raise TradingError(f"{label}已有有效回执，不适用未知订单人工归档；可继续重新核对")
            if archive and leg["dispatch"] != "sending":
                raise TradingError(f"{label}记录为尚未发送，不适用未知订单人工归档；可继续重新核对")
            cid = order.get("newClientOrderId")
            if not isinstance(cid, str) or not re.fullmatch(r"[.A-Za-z0-9_:/-]{1,36}", cid) or cid in cids:
                raise TradingError("原客户端订单编号缺失或重复")
            cids.add(cid)
            before = positive((pending.get("before") or {}).get(side), True)
            owned = positive((state.get("owned") or {}).get(side), True)
            target = positive((pending.get("target") or {}).get(side), True)
            if before != owned or Fraction(target) != Fraction(before) + Fraction(qty):
                raise TradingError("原批次基线或目标与底仓记录不一致，不能人工归档")
        # Budget-rejected repair records do not consume an attempt; legitimate
        # journals may therefore contain more than two legs per attempt.
        for leg in pending["repairs"]:
            if not isinstance(leg, dict) or leg.get("key") not in {"long", "short"}:
                raise TradingError("补偿订单账户方向无效，无法核对")
            order, side = leg.get("order"), leg["key"].upper()
            if (not isinstance(order, dict) or order.get("symbol") != SYMBOL
                    or order.get("positionSide") != side or order.get("side") != ("SELL" if side == "LONG" else "BUY")
                    or order.get("type") != "MARKET" or leg.get("dispatch") not in {"prepared", "sending"}
                    or not 0 < positive(order.get("quantity")) <= qty):
                raise TradingError("补偿委托字段与本批减仓记录不符，无法核对")
            cid = order.get("newClientOrderId")
            if not isinstance(cid, str) or not re.fullmatch(r"[.A-Za-z0-9_:/-]{1,36}", cid) or cid in cids:
                raise TradingError("补偿客户端订单编号缺失或重复")
            cids.add(cid)
            if leg.get("receipt") is not None:
                Executor.validate_receipt(order, leg["receipt"])
        return pending

    def check(self, pair_id):
        """Run the ordinary recovery state machine without sending repair orders."""
        from .pair_execution import PairTrader, PairPositionError
        self.previews.pop(pair_id, None)
        pair = self.store.pair(pair_id)
        if pair is None:
            raise TradingError("配对组不存在")
        state = self.store.get("pair_runtime:" + pair_id, {})
        margin = self.store.get("pair_margin:" + pair_id, {})
        if isinstance(state, dict) and state.get("pending") is None:
            raise TradingError("当前没有待核对批次，可能已由后台完成；请刷新配对组查看结果")
        pending = self._eligible(pair, state, margin, archive=False)
        # Keep the queried batch even if _finish removes it from runtime.
        original = pending
        self.manager._members(pair)
        trader = self.manager.trader or PairTrader(self.engine)
        _, brokers, identities = trader._members(pair)
        if pending.get("identities") != identities or state.get("identities") != identities:
            raise TradingError("原批次与当前真实账户身份不一致，无法核对")
        state.pop("retry_after", None)
        state["api_notice"] = None
        try:
            trader._recover(pair, state, brokers, read_only=True)
        except TradingError as exc:
            trader._retry_delay(state, exc)
            state.update(reason=exchange_reason(str(exc)), api_notice=api_wait_notice(exc))
            if isinstance(exc, PairPositionError):
                state.update(phase="attention", attention=state["reason"])
        finally:
            trader._save(pair, state)
        self.manager._changed(pair)
        current = state.get("pending")
        archive_available, archive_reason = False, "本批已完成核对，无需人工归档"
        if current:
            try:
                self._eligible(pair, state, self.store.get("pair_margin:" + pair_id, {}))
                archive_available, archive_reason = True, "两笔原订单仍无回执；可继续检查历史订单、实际底仓与挂单是否满足人工归档条件"
            except TradingError as exc:
                archive_reason = str(exc)
        rows = []
        for leg in (current or original)["legs"] + (current or original)["repairs"]:
            receipt = leg.get("receipt") or {}
            rows.append({"side": leg["key"].upper(), "client_order_id": leg["order"]["newClientOrderId"],
                         "status": receipt.get("status", "UNKNOWN"), "executed_qty": receipt.get("executedQty"),
                         "error": exchange_reason(receipt.get("reject_reason") or
                                                  (leg.get("error") if receipt.get("status") not in TERMINAL else None))})
        return {"status": "checked", "pair_id": pair_id, "batch_id": original["id"], "checked_at": time.time(),
                "completed": current is None, "message": state.get("reason", "本次核对结束"), "orders": rows,
                "archive_available": archive_available, "archive_reason": archive_reason}

    def _history(self, broker, pending):
        start, end = int((pending["created_at"] - 60) * 1000), int(time.time() * 1000)
        if isinstance(broker, LiveBroker):
            with broker.reconciliation_budget():
                rows = broker.api.call("GET", "/fapi/v3/allOrders", {
                    "symbol": SYMBOL, "startTime": start, "endTime": end, "limit": 1000}, signed=True, weight=5)
        elif isinstance(broker, PaperBroker):
            broker.reload()
            rows = list(broker.state["orders"].values())
        else:
            raise TradingError("当前账户不支持完整历史订单核对")
        if not isinstance(rows, list) or len(rows) >= 1000:
            raise TradingError("历史订单响应不完整或达到 1000 条上限，不能将未找到视为核对依据")
        for row in rows:
            if (not isinstance(row, dict) or row.get("symbol") != SYMBOL
                    or not isinstance(row.get("clientOrderId"), str) or not row["clientOrderId"]):
                raise TradingError("历史订单响应字段无效，不能人工归档")
        return rows, {"start_time": start, "end_time": end, "rows": len(rows)}

    def _read(self, pair_id):
        from .pair_execution import PairTrader
        from .margin_balance import MarginBalancer
        pair = self.store.pair(pair_id)
        if pair is None:
            raise TradingError("配对组不存在")
        state, margin = self.store.get("pair_runtime:" + pair_id, {}), self.store.get("pair_margin:" + pair_id, {})
        pending = self._eligible(pair, state, margin)
        accounts = self.manager._members(pair)
        _, brokers, identities = PairTrader(self.engine)._members(pair)
        if pending.get("identities") != identities or state.get("identities") != identities:
            raise TradingError("原批次与当前真实账户身份不一致，不能人工归档")
        found, evidence = {}, []
        for leg in pending["legs"]:
            broker, order = brokers[leg["key"]], leg["order"]
            row = _query(broker, order)
            source = "original_client_id"
            window = None
            if row is None:
                rows, window = self._history(broker, pending)
                matches = [item for item in rows if item["clientOrderId"] == order["newClientOrderId"]]
                if len(matches) > 1:
                    raise TradingError("历史订单包含重复的原客户端编号，不能人工归档")
                if matches:
                    row = matches[0]
                    Executor.validate_receipt(order, row)
                    timestamp = row.get("time")
                    if (positive(row.get("origQty")) != positive(order["quantity"])
                            or isinstance(broker, LiveBroker) and row.get("type") != "MARKET"
                            or type(timestamp) not in (int, float) or not math.isfinite(timestamp)
                            or not window["start_time"] <= timestamp <= window["end_time"]):
                        raise TradingError("历史原订单的数量、类型或时间不匹配，不能采纳回执")
                    source = "order_history"
            if row is not None:
                found[leg["key"]] = deepcopy(row)
            evidence.append({"side": leg["key"].upper(), "client_order_id": order["newClientOrderId"],
                             "result": "found" if row is not None else "not_found", "source": source,
                             "history": window, "checked_at": time.time()})
        with ExitStack() as budget:
            for broker in brokers.values():
                if isinstance(broker, LiveBroker):
                    budget.enter_context(broker.reconciliation_budget())
            MarginBalancer(self.engine).verify_members(pair)
        snapshots, guards = self.manager._read_members(pair, adopt=True, reconciliation=True)
        actual = {side: wire(snapshots[key].pair(SYMBOL)[0 if side == "LONG" else 1].qty) for key, side in SIDES}
        if not found:
            if any(dec(actual[side]) != dec(pending["before"][side]) for _, side in SIDES):
                raise TradingError("实际持仓未回到本批开仓前底仓；即使两侧数量相等，也不能归档此未决批次")
            if any(snapshot.pair(SYMBOL)[0].leverage != pending["leverage"] for snapshot in snapshots.values()):
                raise TradingError("实际杠杆与原批次不一致，不能人工归档")
            require_archived_orders_clear(self.engine, pair, state=state, brokers=brokers)
        if PairTrader(self.engine)._members(pair)[2] != identities:
            raise TradingError("核对期间账户身份变化，请重新核对")
        return {"pair": pair, "state": state, "margin": margin, "accounts": accounts, "identities": identities,
                "snapshots": snapshots, "guards": guards, "found": found, "evidence": evidence,
                "actual": actual, "fingerprint": self._fingerprint(pair, state, margin, accounts)}

    def _commit(self, read, runtime, message, *, audit=None, expires=None):
        from .pair_execution import PairTrader
        def check_current():
            for _, current in read["guards"].values():
                current()
            if expires is not None and time.monotonic() >= expires:
                raise TradingError("核对预览已过期，请重新核对")
            if PairTrader(self.engine)._members(read["pair"])[2] != read["identities"]:
                raise TradingError("核对期间账户身份发生变化")
        with self.manager._current_members(read["guards"]):
            self.store.commit_pair_recovery(read["pair"], runtime, expected_runtime=read["state"],
                expected_margin=read["margin"], accounts=read["accounts"], check_current=check_current,
                message=message, audit=audit)
        self.manager._changed(read["pair"])

    def _restore_receipts(self, read):
        if not read["found"]:
            return None
        runtime = deepcopy(read["state"])
        for leg in runtime["pending"]["legs"]:
            if leg["key"] in read["found"]:
                leg.update(receipt=read["found"][leg["key"]], error=None)
        message = "已找回原订单真实回执；系统继续核对该批成交与持仓，尚未解除执行限制"
        runtime.update(phase="reconciling", reason=message, updated_at=time.time())
        self._commit(read, runtime, message)
        self.previews.pop(read["pair"]["id"], None)
        return {"status": "receipts_found", "message": message}

    def preview(self, pair_id):
        read = self._read(pair_id)
        restored = self._restore_receipts(read)
        if restored:
            return restored
        pending = read["state"]["pending"]
        token = uuid.uuid4().hex
        review = {"status": "review", "token": token, "pair_id": pair_id, "batch_id": pending["id"],
            "created_at": pending["created_at"], "checked_at": time.time(), "before": pending["before"],
            "actual": read["actual"], "leverage": pending["leverage"], "orders": read["evidence"],
            "message": "原编号和完整时间窗历史均未找到两笔订单，实际持仓等于本批前底仓且无挂单；可人工归档未知记录，保持暂停"}
        self.previews[pair_id] = {"token": token, "expires": time.monotonic() + PREVIEW_TTL,
            "fingerprint": read["fingerprint"], "review": deepcopy(review)}
        return review

    def confirm(self, pair_id, token, acknowledge_unknown=False):
        preview = self.previews.get(pair_id)
        if acknowledge_unknown is not True:
            raise TradingError("请明确确认已在交易所核对订单与持仓，并接受归档记录仍未取得订单终态")
        if not preview or preview["token"] != token or time.monotonic() >= preview["expires"]:
            raise TradingError("核对预览已失效，请重新核对订单与持仓")
        read = self._read(pair_id)
        if read["fingerprint"] != preview["fingerprint"]:
            self.previews.pop(pair_id, None)
            raise TradingError("订单、底仓或账户配置已变化，请重新核对")
        restored = self._restore_receipts(read)
        if restored:
            raise TradingError(restored["message"])
        from .engine import snapshot_json
        from .pair_execution import empty_progress
        runtime, pending = deepcopy(read["state"]), read["state"]["pending"]
        watches = deepcopy((runtime.get("recovery_watch") or {}).get("batches", []))
        if len(watches) >= MAX_WATCH_BATCHES:
            raise TradingError("待观察的人工归档批次已达上限，请先处理原订单记录")
        watches.append({"id": pending["id"], "created_at": pending["created_at"], "identities": read["identities"],
                        "legs": [{"key": leg["key"], "order": deepcopy(leg["order"])} for leg in pending["legs"]]})
        message = "已人工归档未取得回执的普通开仓批次，底仓保留；配对组仍暂停，可点击启动重新核验"
        audit = {"status": "manual_archived_unresolved", "pending": deepcopy(pending), "confirmed_at": time.time(),
                 "preview": deepcopy(preview["review"]), "confirmation": read["evidence"], "actual": read["actual"]}
        runtime.update(pending=None, phase="paused", reason=message, recovery_watch={"batches": watches},
            progress=empty_progress(runtime["owned"], (runtime.get("progress") or {}).get("completed_cycles", 0)),
            snapshots={key: snapshot_json(snapshot, [SYMBOL]) for key, snapshot in read["snapshots"].items()},
            updated_at=time.time())
        runtime.pop("attention", None)
        runtime.pop("retry_after", None)
        self._commit(read, runtime, message, audit=audit, expires=preview["expires"])
        self.previews.pop(pair_id, None)
        return {"ok": True, "message": message}
