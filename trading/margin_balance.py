"""USD1 transfers between verified subaccounts; never retry an uncertain write.

The caller holds the pair lock and both member-account locks. Transfer identity
comes from authenticated Aster responses, never a locally supplied account name.
"""
from contextlib import ExitStack, contextmanager, nullcontext
from copy import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import math
import re
import time
import uuid
from urllib.parse import urlencode

from eth_account import Account as EthAccount
from eth_account.messages import encode_typed_data

from .exchange import API, AmbiguousOrder, ExchangeError, LiveBroker, RequestNotSent, api_wait_notice, credentials_for
from .models import TradingError, dec, decimal_value, positive, wire
from .pair_planning import PairRecoveryConflict


DEFAULT_MARGIN = {"enabled": False, "master_env_prefix": "", "check_interval_seconds": 5,
    "threshold": "10", "min_transfer": "1", "max_transfer": "1000", "buffer_ratio": "0.05",
    "cooldown_seconds": 30}
TRANSFER_TYPES = frozenset({"TRANSFER", "SUBUSER_ASSET_TRANSFER"})
TRANSFER_UNIT = Fraction(Decimal("0.00000001"))
TRANSFER_WS_GRACE_SECONDS = 2
REJECTION_CODES = frozenset({-1002, -1003, -1011, -1015, -1020, -1022, -1100, -1101,
    -1102, -1103, -1104, -1105, -1106, -1111, -1130, -2014, -2015, -2019})


def validate_margin(config):
    if not isinstance(config, dict) or set(config) - set(DEFAULT_MARGIN):
        raise TradingError("保证金平衡配置字段无效")
    value = {**DEFAULT_MARGIN, **config}
    if type(value["enabled"]) is not bool:
        raise TradingError("保证金平衡开关必须是布尔值")
    prefix = value["master_env_prefix"]
    if not isinstance(prefix, str) or (prefix and not re.fullmatch(r"[A-Z][A-Z0-9_]{1,40}", prefix)):
        raise TradingError("主账户凭据变量前缀无效")
    for name, low, high in (("check_interval_seconds", 1, 3600), ("cooldown_seconds", 1, 86400)):
        number = value[name]
        if type(number) not in (int, float) or not math.isfinite(number) or not low <= number <= high:
            raise TradingError("保证金检查间隔或划转冷却时间无效")
    for name in ("threshold", "min_transfer", "max_transfer", "buffer_ratio"):
        amount = positive(value[name], allow_zero=name in {"threshold", "buffer_ratio"})
        if amount > 1000000000 or (name == "buffer_ratio" and amount >= 1):
            raise TradingError("保证金平衡金额或风险缓冲无效")
        value[name] = wire(amount)
    if dec(value["max_transfer"]) < dec(value["min_transfer"]) or dec(value["min_transfer"]) < Decimal("0.00000001"):
        raise TradingError("最大划转金额不得小于最小金额，最小金额至少为 0.00000001 USD1")
    return value


class TransferAPI(API):
    """Only transfer mutations use signer-only auth; ordinary API is unchanged."""

    def __init__(self, credentials=None, transport=None, budget=None, *, before_submit=None):
        super().__init__(credentials, transport=transport, budget=budget)
        self.before_submit = before_submit
        self.http.event_hooks["request"].append(self._before_request)

    def _before_request(self, request):
        # HTTPX calls request hooks after budget admission/signing and before
        # transport dispatch. A revoked read here is definitively NOT sent.
        if request.method != "GET" and self.before_submit is not None:
            try:
                self.before_submit()
            except Exception:
                raise RequestNotSent("划转发送前账户状态已变化，未发送请求") from None

    def signed_parameters(self, params):
        if not self.credentials or any(key in params for key in ("user", "signer", "nonce", "signature")):
            raise RequestNotSent("划转签名参数无效，未发送请求")
        with self.nonce_lock:
            self.last_nonce = max(time.time_ns() // 1000, self.last_nonce + 1)
            nonce = self.last_nonce
        data = {**params, "nonce": str(nonce), "signer": self.credentials["signer"]}
        try:
            message = encode_typed_data(domain_data={"name": "AsterSignTransaction", "version": "1",
                "chainId": 1666, "verifyingContract": "0x" + "00" * 20},
                message_types={"Message": [{"name": "msg", "type": "string"}]},
                message_data={"msg": urlencode(data)})
            signature = EthAccount.sign_message(message, self.credentials["private_key"]).signature.hex()
        except Exception:
            raise RequestNotSent("划转无法在本地签名，未发送请求") from None
        data["signature"] = signature if signature.startswith("0x") else "0x" + signature
        return data


def _identifier(value):
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,100}", value):
        return value
    return None


def _address(row):
    addresses = {str(row[key]).lower() for key in ("address", "sourceAddr", "subSourceAddr", "walletAddress")
                 if row.get(key) is not None}
    if len(addresses) > 1 or any(not re.fullmatch(r"0x[0-9a-f]{40}", address) for address in addresses):
        raise TradingError("子账户列表的钱包地址字段冲突或无效")
    return next(iter(addresses), None)


def _public_record(record):
    if not isinstance(record, dict) or not record:
        return None
    return {key: record[key] for key in ("request_id", "source", "destination", "amount", "created_at",
        "status", "confirmed_at", "acknowledged_at", "refreshed_at", "refresh_source", "transaction_id", "income_type") if key in record}


def _display_amount(value):
    try:
        return wire(value)
    except (TradingError, ArithmeticError, TypeError, ValueError):
        return "未确认"


def _remaining_time(deadline, now):
    if type(deadline) not in (int, float) or type(now) not in (int, float):
        return ""
    try:
        remaining = deadline - now
        if not math.isfinite(remaining):
            return ""
        return f"（本次检查时剩余约 {max(0, math.ceil(remaining))} 秒）"
    except (ArithmeticError, ValueError):
        return ""


class MarginBalancer:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store
        self.refreshed_snapshots = None

    def _members(self, pair):
        members = {side: self.store.account(pair[side + "_account_id"]) for side in ("long", "short")}
        if not all(members.values()) or members["long"]["id"] == members["short"]["id"]:
            raise TradingError("保证金平衡需要两个存在且不同的子账户")
        if len({member["mode"] for member in members.values()}) != 1:
            raise TradingError("模拟与实盘账户不能互相划转")
        return members

    @staticmethod
    def _valid_pending(pending):
        if (not isinstance(pending, dict) or not pending
                or pending.get("status") not in {"submitting", "acknowledged", "accepted", "unknown"}
                or {pending.get("source"), pending.get("destination")} != {"long", "short"}
                or not _identifier(pending.get("request_id")) or not isinstance(pending.get("identity"), str)
                or type(pending.get("created_at")) not in (int, float)
                or not math.isfinite(pending["created_at"]) or pending["created_at"] <= 0):
            return False
        try:
            positive(pending.get("amount"))
        except TradingError:
            return False
        return True

    @staticmethod
    def resume_ready(state):
        """A balance refresh enables reserved trading, never another transfer."""
        pending = state.get("pending") if isinstance(state, dict) else None
        if not MarginBalancer._valid_pending(pending) or pending["status"] != "unknown":
            return False
        stamp = pending.get("trading_baseline_at")
        return (type(stamp) in (int, float) and math.isfinite(stamp)
                and pending["created_at"] <= stamp <= time.time())

    def _verify_pending_identity(self, pair, members, pending):
        if members["long"]["mode"] != "live":
            raise TradingError("模拟划转出现未完成日志，需要核对")
        with self._master(pair, members) as (_, master, children):
            if pending.get("identity") != self._fingerprint(pair, members, children, master):
                raise TradingError("划转待核对记录与当前账户身份不一致")

    def risk_snapshots(self, pair, snapshots, *, recovery=False):
        """Reserve an uncertain outgoing debit on copies of fresh real reads."""
        state = self.store.get("pair_margin:" + pair["id"], {})
        if not isinstance(state, dict):
            raise TradingError("保证金划转日志无效")
        pending = state.get("pending")
        if pending is None:
            return snapshots
        if not self._valid_pending(pending):
            raise TradingError("保证金划转待核对记录无效")
        if pending["status"] not in {"unknown", "submitting"}:
            if recovery:
                return snapshots
            raise TradingError("划转尚未完成余额刷新，暂不新增")
        if not recovery and not self.resume_ready(state):
            raise TradingError("未知划转等待新的保证金基线，暂不新增")
        self._verify_pending_identity(pair, self._members(pair), pending)
        for snapshot in snapshots.values():
            snapshot.require_fresh()
            if not recovery and snapshot.timestamp < pending["trading_baseline_at"]:
                raise TradingError("账户快照早于保证金恢复基线，等待新快照")
        result = dict(snapshots)
        source = pending["source"]
        result[source] = copy(snapshots[source])
        for field in ("available", "equity", "wallet"):
            setattr(result[source], field, decimal_value(
                Fraction(dec(getattr(snapshots[source], field))) - Fraction(positive(pending["amount"])), exact=True))
        return result

    def _resume_unknown(self, pair, members, state, config):
        pending = state["pending"]
        if not self.resume_ready(state):
            symbols = [pair.get("symbol", "XAUUSD1")]
            brokers = {side: self.engine.broker(member) for side, member in members.items()}
            self._require_budget(members, {side: broker.margin_snapshot_weight(symbols, fresh_modes=True)
                for side, broker in brokers.items()}, reconciliation=True)
            snapshots = {}
            for side, broker in brokers.items():
                with getattr(broker, "reconciliation_budget", nullcontext)():
                    snapshots[side] = broker.margin_snapshot(symbols, fresh_modes=True)
                snapshots[side].require_fresh()
                if snapshots[side].timestamp < pending["created_at"]:
                    raise TradingError("保证金恢复查询返回划转前快照，等待新余额")
            with ExitStack() as locks:
                for side in sorted(brokers, key=lambda value: members[value]["id"]):
                    locks.enter_context(brokers[side]._snapshot_lock)
                for side, broker in brokers.items():
                    broker.require_snapshot_current(snapshots[side])
                self._check_snapshots(pair, snapshots)
                pending["trading_baseline_at"] = min(value.timestamp for value in snapshots.values())
                self._record_success(pair, state)
                self.refreshed_snapshots = snapshots
            self._invalidate(members)
        elif state.get("blocked_reason") or state.get("api_notice"):
            self._record_success(pair, state)
        return self._view(state, config, "unknown",
            f"划转结果仍未知；已读取新的保证金基线，交易可继续按最新余额检查。转出侧额外预留 {pending['amount']} USD1；保留原请求，只读核对，不重发或发起新划转")

    def _require_budget(self, members, weights, *, reconciliation=False):
        """Admit a complete read group before spending on its first account."""
        groups = {}
        for side, weight in weights.items():
            broker = self.engine.broker(members[side])
            budget = getattr(getattr(broker, "api", None), "budget", None)
            if budget is not None and weight:
                group = groups.setdefault(id(budget), [broker, budget, 0])
                group[2] += weight
        for broker, budget, weight in groups.values():
            priority = getattr(broker, "reconciliation_budget", nullcontext)() if reconciliation else nullcontext()
            with priority:
                budget.require_available(weight)

    def _live_budget_weights(self, pair, members, snapshots, snapshot_guards=None):
        # Listing and POST use the long member's shared budget. Each member
        # additionally needs its account and transfer income baseline (5+30).
        weights = {"long": 45, "short": 35}
        for side, member in members.items():
            if (type(getattr(snapshots[side], "account_read_generation", None)) is not int
                    and not callable((snapshot_guards or {}).get(side))):
                weights[side] += self.engine.broker(member).snapshot_weight([pair.get("symbol", "XAUUSD1")])
        watch = (self.store.get("pair_runtime:" + pair["id"], {}) or {}).get("recovery_watch")
        if watch is not None:
            from .pair_recovery import MAX_WATCH_BATCHES
            batches = watch.get("batches") if isinstance(watch, dict) else None
            if not isinstance(batches, list) or not 1 <= len(batches) <= MAX_WATCH_BATCHES:
                raise TradingError("人工归档订单跟踪记录无效，禁止新开仓与划转")
            for batch in batches:
                legs = batch.get("legs") if isinstance(batch, dict) else None
                if (not isinstance(legs, list) or len(legs) != 2
                        or any(not isinstance(leg, dict) for leg in legs)
                        or {leg.get("key") for leg in legs} != {"long", "short"}):
                    raise TradingError("人工归档订单明细无效，禁止新开仓与划转")
                for leg in legs:
                    weights[leg["key"]] += 1
        return weights

    @contextmanager
    def _master(self, pair, members):
        config = validate_margin(pair.get("margin", {}))
        prefix = config["master_env_prefix"]
        if not prefix or prefix in {member["env_prefix"] for member in members.values()}:
            raise TradingError("请配置独立的主账户 API signer 环境变量前缀")
        master = credentials_for(prefix)
        children = {side: credentials_for(member["env_prefix"]) for side, member in members.items()}
        users = [value["user"].lower() for value in children.values()]
        signers = [value["signer"].lower() for value in children.values()]
        if len(set(users)) != 2 or master["user"].lower() in users:
            raise TradingError("划转成员必须是主账户下两个不同子账户")
        if master["signer"].lower() in signers or master["signer"].lower() == master["user"].lower():
            raise TradingError("划转必须使用独立授权的主账户 API signer")
        budget = getattr(getattr(self.engine.broker(members["long"]), "api", None), "budget", None)
        api = API(master, budget=budget)
        try:
            yield api, master, children
        finally:
            api.close()

    def _account_rows(self, members):
        return {side: self.engine.broker(member).api.call("GET", "/fapi/v3/accountWithJoinMargin",
            signed=True, weight=5) for side, member in members.items()}

    @staticmethod
    def _verify(listing, children, accounts):
        if not isinstance(listing, list) or not listing or any(not isinstance(row, dict) for row in listing):
            raise TradingError("主账户子账户列表响应无效，无法确认归属")
        if any(type(row.get("parentAccount")) is not bool for row in listing):
            raise TradingError("子账户列表缺少明确的主子账户标记")
        parents = [row for row in listing if row["parentAccount"]]
        if len(parents) != 1:
            raise TradingError("子账户列表未唯一标识主账户")
        ids = [_identifier(row.get("accountId")) for row in listing]
        if any(value is None for value in ids) or len(set(ids)) != len(ids):
            raise TradingError("子账户列表包含缺失或重复的账户标识")
        found = []
        for side, credentials in children.items():
            address = credentials["user"].lower()
            raw = accounts.get(side)
            if not isinstance(raw, dict):
                raise TradingError("子账户信息响应无效，无法确认归属")
            raw_id = _identifier(raw.get("accountId"))
            matches = [row for row in listing if _address(row) == address]
            if not matches and raw_id:
                matches = [row for row in listing if _identifier(row.get("accountId")) == raw_id]
            if len(matches) != 1 or matches[0]["parentAccount"]:
                raise TradingError("无法核实同主账户归属：需要子账户列表地址，或账户信息中的 accountId 与列表一致")
            row = matches[0]
            if raw_id and raw_id != _identifier(row["accountId"]):
                raise TradingError("子账户地址与账户标识不一致")
            if _address(row) is not None and _address(row) != address:
                raise TradingError("子账户标识对应的钱包地址不一致")
            if row.get("status", row.get("subAccountStatus")) in {"FROZEN", "FREEZE"}:
                raise TradingError("子账户已冻结，禁止划转")
            found.append(_identifier(row["accountId"]))
        if len(set(found)) != 2:
            raise TradingError("两个凭据解析为同一个子账户")

    def verify_members(self, pair):
        """Read-only ownership check used when enabling a live pair."""
        members = self._members(pair)
        if members["long"]["mode"] == "paper":
            return {"verified": True, "mode": "paper"}
        with self._master(pair, members) as (api, master, children):
            listing = api.call("GET", "/fapi/v3/getSubAccountList", signed=True, weight=5)
            self._verify(listing, children, self._account_rows(members))
        return {"verified": True, "mode": "live"}

    @staticmethod
    def _fingerprint(pair, members, children=None, master=None):
        binding = {"pair": pair["id"], "symbol": pair.get("symbol", "XAUUSD1"),
            "members": {side: {"id": member["id"], "mode": member["mode"], "prefix": member["env_prefix"]}
                        for side, member in members.items()}}
        if children is not None:
            binding["users"] = {side: value["user"].lower() for side, value in children.items()}
        if master is not None:
            binding["master"] = master["user"].lower()
        return hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _check_snapshots(pair, snapshots, *, require_orders=False):
        if set(snapshots) != {"long", "short"}:
            raise TradingError("保证金平衡缺少两侧账户快照")
        for side, snapshot in snapshots.items():
            snapshot.require_fresh()
            snapshot.require_modes([pair.get("symbol", "XAUUSD1")])
            if snapshot.hedge_mode is not True or snapshot.multi_assets is not False:
                raise TradingError("保证金平衡要求双向持仓协议、USD1 单币全仓模式，每子账户只持指定方向")
            if (not snapshot.can_trade or snapshot.equity <= 0 or snapshot.open_orders
                    or (require_orders and snapshot.open_orders is None)):
                raise TradingError("账户权限、权益或未完成委托不满足划转条件")
            for position in snapshot.positions:
                if dec(position.qty) < 0 or (position.qty and (position.symbol != pair.get("symbol", "XAUUSD1")
                        or position.side != side.upper() or position.isolated)):
                    raise TradingError("子账户存在隔离保证金、非指定品种或反向持仓")
            for number in (snapshot.available, snapshot.wallet, snapshot.equity, snapshot.maintenance):
                dec(number)

    @staticmethod
    def _balance_delta(snapshots):
        return Fraction(dec(snapshots["long"].available)) - Fraction(dec(snapshots["short"].available))

    @staticmethod
    def _may_need_transfer(snapshots, config):
        # This upper bound can only skip a new transfer. A candidate still needs
        # ownership, withdrawal, risk, order and revocable-read checks below.
        difference = abs(MarginBalancer._balance_delta(snapshots))
        if difference <= Fraction(dec(config["threshold"])):
            return False
        units = difference // (2 * TRANSFER_UNIT)
        return units * TRANSFER_UNIT >= Fraction(dec(config["min_transfer"]))

    @staticmethod
    def _no_transfer_reason(snapshots, config, diagnostics=None):
        """Explain a rejected plan using the same already-validated read only."""
        difference = abs(MarginBalancer._balance_delta(snapshots))
        threshold = Fraction(dec(config["threshold"]))
        minimum = Fraction(dec(config["min_transfer"]))
        detail = f"本次检查两侧可用余额差 {_display_amount(difference)} USD1"
        if difference <= threshold:
            detail += f"，未超过划转门槛 {_display_amount(threshold)} USD1（须严格大于）"
        else:
            rounded_half = (difference // (2 * TRANSFER_UNIT)) * TRANSFER_UNIT
            if rounded_half < minimum:
                detail += (f"；差额一半按划转精度向下取整为 {_display_amount(rounded_half)} USD1，"
                           f"低于最小划转额 {_display_amount(minimum)} USD1")
            elif diagnostics:
                detail += "；" + diagnostics["reason"]
            else:
                detail += (f"；来源账户按可划余额、单次限额、现金与风险缓冲共同约束后，"
                           f"按划转精度取整后的安全可划金额低于最小划转额 {_display_amount(minimum)} USD1")
        return detail + "；本次不划转，后续按检查间隔重新评估"

    @staticmethod
    def _plan(pair, members, snapshots, config, withdrawable, *, occupied_floors=None, diagnostics=None):
        delta = MarginBalancer._balance_delta(snapshots)
        if abs(delta) <= dec(config["threshold"]):
            return None
        source, destination = ("long", "short") if delta > 0 else ("short", "long")
        snapshot = snapshots[source]
        buffer = Fraction(dec(config["buffer_ratio"]))
        # Paired transfers use the group's policy, just like paired opening.
        # Standalone account limits must not silently override group settings.
        ordinary = pair.get("ordinary")
        if not isinstance(ordinary, dict) or "margin_limit" not in ordinary:
            raise TradingError("编组基础保证金占用上限缺失")
        base_limit = positive(ordinary["margin_limit"])
        if base_limit > 1:
            raise TradingError("编组基础保证金占用上限不得超过 100%")
        limit = Fraction(base_limit) - buffer
        if limit <= 0:
            raise TradingError("风险缓冲必须小于编组基础保证金占用上限")
        equity = Fraction(positive(snapshot.equity))
        # Keep both spare cash and enough post-transfer equity for ALL occupied
        # margin. Available funds are not synonymous with safely transferable funds.
        occupied = max(snapshot.occupied_margin_exact, (occupied_floors or {}).get(source, Fraction(0)))
        risk_room = equity - max(occupied, Fraction(snapshot.maintenance)) / limit
        cash_room = Fraction(snapshot.available) - equity * buffer
        constraints = {"余额差的一半": Fraction(abs(delta)) / 2,
            "单次上限": Fraction(dec(config["max_transfer"])),
            "来源账户可划余额": Fraction(positive(withdrawable[source], True)),
            "现金保留": cash_room, "风险缓冲": risk_room}
        amount = min(constraints.values())
        amount = max(Fraction(0), amount)
        units = amount // TRANSFER_UNIT
        rounded = Decimal(int(units)) * Decimal("0.00000001")
        if rounded < dec(config["min_transfer"]):
            if diagnostics is not None:
                def percent(value):
                    return wire(Decimal(int(Fraction(value) * 10000)) * Decimal("0.01"))
                def cash(value):
                    return wire(Decimal(int(max(Fraction(0), value) // TRANSFER_UNIT)) * Decimal("0.00000001"))
                limited = "、".join(f"{name}（可划 {cash(value)} USD1）" for name, value in constraints.items()
                    if max(Fraction(0), value) // TRANSFER_UNIT * TRANSFER_UNIT < dec(config["min_transfer"]))
                side = "A 多侧" if source == "long" else "B 空侧"
                maintenance = (f"，维持保证金率约 {percent(Fraction(snapshot.maintenance) / equity)}%"
                    if Fraction(snapshot.maintenance) > occupied else "")
                diagnostics["reason"] = (
                    f"转出侧 {side}占用率约 {percent(occupied / equity)}%{maintenance}，"
                    f"划转后占用上限 {percent(limit)}%（编组基础 {percent(base_limit)}%，再扣 {percent(buffer)} 个百分点；"
                    "不含普通高杠杆或循环额外的 5 个百分点）；"
                    f"限制项：{limited}；共同约束后的安全可划金额 {wire(rounded)} USD1，"
                    f"低于最小划转额 {_display_amount(Fraction(dec(config['min_transfer'])))} USD1")
            return None
        return {"source": source, "destination": destination, "amount": wire(rounded)}

    @staticmethod
    def _view(state, config, status, reason, *, blocks=False, plan=None):
        notice = state.get("api_notice")
        notice = ({"kind": notice["kind"], "text": notice["text"]}
                  if isinstance(notice, dict) and notice.get("kind") in {"budget", "cooldown", "rate_limit"}
                  and isinstance(notice.get("text"), str) and notice["text"] else None)
        resumed = (MarginBalancer.resume_ready(state) and not blocks
                   and (not state.get("blocked_reason") or state.get("retry_without_blocking")))
        result = {"enabled": config["enabled"], "status": status, "reason": reason,
            "blocks_trading": bool(blocks or state.get("pending") and not resumed),
            "trading_resume_allowed": resumed, "checked_at": state.get("checked_at"),
            "pending": _public_record(state.get("pending")), "last_transfer": _public_record(state.get("last_transfer")),
            "plan": plan, "next_check_at": state.get("next_check_at", 0), "cooldown_until": state.get("cooldown_until", 0),
            "api_notice": notice}
        deadline = state.get("next_check_at", 0)
        if notice and type(deadline) in (int, float) and math.isfinite(deadline):
            result["retry_after"] = max(0, deadline - time.time())
        return result

    @staticmethod
    def status_view(pair, journal, runtime_view=None):
        """Project the durable journal without letting an older view hide a write."""
        config = validate_margin(pair.get("margin", {}))
        if journal is None:
            journal = {}
        if not isinstance(journal, dict):
            return MarginBalancer._view({}, config, "blocked", "保证金划转日志无效，停止新开仓和新划转；请人工核对服务器日志与交易所划转记录", blocks=True)
        view = lambda status, reason, **kwargs: MarginBalancer._view(journal, config, status, reason, **kwargs)
        pending = journal.get("pending")
        if pending is not None:
            if not isinstance(pending, dict) or pending.get("status") not in {"submitting", "unknown", "accepted", "acknowledged"}:
                return view("blocked", "保证金划转待核对记录无效，停止新开仓和新划转；请人工核对服务器日志与交易所划转记录", blocks=True)
            status = "unknown" if pending["status"] == "submitting" else pending["status"]
            if MarginBalancer.resume_ready(journal) and (not journal.get("blocked_reason") or journal.get("retry_without_blocking")):
                return view("unknown", f"划转仍待核对，交易按最新余额继续检查；转出侧额外预留 {pending['amount']} USD1，不重发或新增划转")
            reasons = {"unknown": "划转结果未知，保留原记录供只读核对；缺少交易编号时需人工核对交易所流水。停止新开仓和新划转，不会重发原请求",
                "accepted": "划转请求已受理，系统继续查询两侧 USD1 流水；核实前不开始新开仓或新划转",
                "acknowledged": "交易所已确认划转，系统继续读取两侧最新余额；刷新前不开始新开仓或新划转"}
            return view(status, journal.get("blocked_reason") or reasons[status], blocks=True)
        if not config["enabled"]:
            return view("disabled", "自动保证金平衡未启用")
        if not pair.get("enabled"):
            return view("paused", "配对组已暂停，不发起新划转；已有划转仍继续只读核对，需启动配对组后才重新评估新划转")
        if journal.get("blocked_reason"):
            return view("waiting" if journal.get("retry_without_blocking") else "blocked", journal["blocked_reason"],
                        blocks=not journal.get("retry_without_blocking"))
        for field in ("checked_at", "next_check_at", "cooldown_until"):
            value = journal.get(field)
            if field in journal and (type(value) not in (int, float) or not math.isfinite(value)):
                return view("blocked", "保证金划转日志时间无效", blocks=True)
        if time.time() < journal.get("cooldown_until", 0):
            return view("cooldown", "划转冷却中" + _remaining_time(journal["cooldown_until"], time.time())
                        + "；结束后重新评估划转条件")
        # An execution view can explain a current check, but cannot resurrect a
        # pending write, reuse an old transfer, or override changed configuration.
        if (isinstance(runtime_view, dict) and runtime_view.get("enabled") == config["enabled"]
                and runtime_view.get("checked_at") == journal.get("checked_at")
                and runtime_view.get("pending") is None
                and runtime_view.get("last_transfer") == _public_record(journal.get("last_transfer"))
                and runtime_view.get("status") in {"waiting", "confirmed", "acknowledged", "paper_confirmed", "rejected", "blocked"}
                and isinstance(runtime_view.get("reason"), str)):
            plan = runtime_view.get("plan")
            plan = {key: plan[key] for key in ("source", "destination", "amount") if key in plan} if isinstance(plan, dict) else None
            return view(runtime_view["status"], runtime_view["reason"], blocks=runtime_view.get("blocks_trading") is True, plan=plan)
        return view("waiting", "等待系统下一次保证金检查" + _remaining_time(journal.get("next_check_at"), time.time())
                    + "；满足余额差额及安全可划条件后才发起划转")

    def _income(self, members, start, end, *, reconciliation=False):
        self._require_budget(members, {side: 30 for side in members}, reconciliation=reconciliation)
        result = {}
        for side, member in members.items():
            broker = self.engine.broker(member)
            priority = getattr(broker, "reconciliation_budget", nullcontext)() if reconciliation else nullcontext()
            with priority:
                rows = broker.api.call("GET", "/fapi/v3/income",
                    {"startTime": start, "endTime": end, "limit": 1000}, signed=True, weight=30)
            if not isinstance(rows, list) or len(rows) >= 1000 or any(not isinstance(row, dict) for row in rows):
                raise TradingError("划转流水不完整，无法核对")
            result[side] = rows
        return result

    def _reconcile(self, pair, members, state, config):
        pending = state["pending"]
        if pending.get("status") == "submitting":
            pending["status"] = "unknown"
            state["last_transfer"] = pending
            self.store.put("pair_margin:" + pair["id"], state)
        if pending.get("status") == "unknown" and not pending.get("transaction_id"):
            return self._resume_unknown(pair, members, state, config)
        if pending.get("status") == "unknown" and not self.resume_ready(state):
            return self._resume_unknown(pair, members, state, config)
        start = int(pending["created_at"] * 1000) // 1000 * 1000
        end = int((pending["created_at"] + 90) * 1000)
        rows = self._income(members, start, end, reconciliation=True)
        self._record_success(pair, state)
        matches = []
        for side, sign in ((pending["source"], -1), (pending["destination"], 1)):
            found = set()
            for row in rows[side]:
                txn = _identifier(row.get("tranId"))
                kind = row.get("incomeType")
                if row.get("asset") != "USD1" or kind not in TRANSFER_TYPES or not txn:
                    continue
                if pending.get("transaction_id") and txn != pending["transaction_id"]:
                    continue
                if [kind, txn] in pending.get("before_receipts", {}).get(side, []):
                    continue
                if [kind, txn] in state.get("used_receipts", []):
                    continue
                try:
                    stamp = dec(row.get("time"))
                    amount = dec(row.get("income"))
                except TradingError:
                    continue
                if start <= stamp <= end and amount == dec(pending["amount"]) * sign:
                    found.add((kind, txn))
            matches.append(found)
        common = matches[0] & matches[1]
        if len(common) != 1 or any(len(values) != 1 for values in matches):
            if pending["status"] == "unknown":
                return self._resume_unknown(pair, members, state, config)
            return self._view(state, config, pending["status"], "系统继续只读查询两侧 USD1 划转流水，需交易编号相同、金额对应且均在核对时间范围内；确认前不开始新开仓或新划转")
        kind, txn = common.pop()
        done = {**pending, "status": "confirmed", "confirmed_at": time.time(), "transaction_id": txn, "income_type": kind}
        state.update(pending=None, last_transfer=done)
        state["used_receipts"] = (state.get("used_receipts", []) + [[kind, txn]])[-100:]
        self.store.put("pair_margin:" + pair["id"], state)
        self._invalidate(members)
        return self._view(state, config, "confirmed", "划转已由两侧流水核实，系统重新读取账户余额；取得新快照前不开始新开仓或新划转", blocks=True)

    def _refresh_acknowledged(self, pair, members, state, config):
        """Official success is final; refresh balances without another write.

        Aster documents code=200/msg=success, not a transaction ID. Do not turn
        that successful response into an ambiguous result merely because income
        IDs may differ between accounts. A timeout never enters this path.
        """
        pending = state["pending"]
        acknowledged_at = pending.get("acknowledged_at")
        if type(acknowledged_at) not in (int, float) or not math.isfinite(acknowledged_at):
            raise TradingError("划转成功回执缺少有效时间，保留待核对状态")
        symbols = [pair.get("symbol", "XAUUSD1")]
        brokers = {side: self.engine.broker(member) for side, member in members.items()}
        done = self._refresh_acknowledged_ws(pair, members, brokers, state)
        if done is not None:
            self._invalidate(members)
            return self._view(state, config, "acknowledged", "交易所回执确认，两侧 WebSocket 余额已核对；等待下一轮账户快照",
                blocks=True, plan={key: done[key] for key in ("source", "destination", "amount")})
        # Give an already connected pair one bounded, non-blocking opportunity
        # to deliver events. The deadline never moves on retries; disconnected
        # or restarted sessions immediately use the ordinary REST fallback.
        checkpoints = pending.get("ws_checkpoints")
        deadline = acknowledged_at + TRANSFER_WS_GRACE_SECONDS
        now = time.time()
        if (pending.get("acknowledgement_code") == 200 and now < deadline
                and isinstance(checkpoints, dict) and set(checkpoints) == set(brokers)
                and all(callable(getattr(broker, "transfer_ws_checkpoint_current", None))
                        and broker.transfer_ws_checkpoint_current(checkpoints[side])
                        for side, broker in brokers.items())):
            state["next_check_at"] = min(now + 1, deadline)
            self._record_success(pair, state)
            return self._view(state, config, "acknowledged", "交易所已确认划转，等待两侧 WebSocket 余额；最多 2 秒后改用 REST 刷新",
                blocks=True)
        self._require_budget(members, {side: broker.margin_snapshot_weight(symbols, fresh_modes=True)
                                     for side, broker in brokers.items()}, reconciliation=True)
        snapshots = {}
        for side, broker in brokers.items():
            with getattr(broker, "reconciliation_budget", nullcontext)():
                current = broker.margin_snapshot(symbols, fresh_modes=True)
                current.require_fresh()
                if current.timestamp < acknowledged_at:
                    raise TradingError("划转后的账户查询返回旧快照，继续等待新余额")
                broker.require_snapshot_current(current)
                snapshots[side] = current
        with ExitStack() as locks:
            for side in sorted(brokers, key=lambda value: members[value]["id"]):
                locks.enter_context(brokers[side]._snapshot_lock)
            for side, broker in brokers.items():
                broker.require_snapshot_current(snapshots[side])
            done = {**pending, "refreshed_at": time.time(), "refresh_source": "rest"}
            state.update(pending=None, last_transfer=done)
            self._record_success(pair, state)
        self._invalidate(members)
        return self._view(state, config, "acknowledged", "交易所回执确认，余额已刷新；等待下一轮账户快照", blocks=True,
            plan={key: done[key] for key in ("source", "destination", "amount")})

    def _refresh_acknowledged_ws(self, pair, members, brokers, state):
        """Balance evidence only refreshes an explicit REST success receipt."""
        pending = state["pending"]
        checkpoints = pending.get("ws_checkpoints")
        if (pending.get("status") != "acknowledged" or pending.get("acknowledgement_code") != 200
                or not isinstance(checkpoints, dict)
                or set(checkpoints) != set(brokers)):
            return None
        balances = {}
        for side, broker in brokers.items():
            read = getattr(broker, "transfer_ws_balance", None)
            if not callable(read):
                return None
            sign = -1 if side == pending["source"] else 1
            result = read(checkpoints[side], dec(pending["amount"]) * sign, pending["created_at"])
            if result is None:
                return None
            balances[side] = result
        with ExitStack() as locks:
            for side in sorted(brokers, key=lambda value: members[value]["id"]):
                locks.enter_context(brokers[side]._snapshot_lock)
            try:
                for _, require_current in balances.values():
                    require_current()
            except TradingError:
                return None
            # Session tokens remain in the private checkpoint only. Store a
            # small, credential-free audit, never a raw private account event.
            evidence = {side: {key: value[key] for key in ("event_cursor", "event_time", "transaction_time",
                "reason", "asset", "delta", "wallet") if key in value} for side, (value, _) in balances.items()}
            done = {**pending, "refreshed_at": time.time(), "refresh_source": "websocket", "balance_evidence": evidence}
            state.update(pending=None, last_transfer=done)
            self._record_success(pair, state)
        return done

    def _invalidate(self, members):
        for member in members.values():
            broker = self.engine.broker(member)
            callback = getattr(broker, "invalidate_cycle_hot_data", None)
            if callback:
                callback("子账户保证金划转，重新读取余额")

    def _paper(self, pair, members, snapshots, state, pending):
        key = "pair_margin:" + pair["id"]
        amount = dec(pending["amount"])
        completed = {**pending, "status": "paper_confirmed", "confirmed_at": time.time()}
        state.update(pending=None, last_transfer=completed)
        # All wallets and the receipt share ONE SQLite transaction. A crash or
        # disk failure cannot publish just one side, even across Store instances.
        with self.store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            wallets = {}
            for side, member in members.items():
                row = db.execute("SELECT data FROM kv WHERE key=?", ("paper:" + member["id"],)).fetchone()
                if row is None:
                    raise TradingError("模拟账户账本不存在")
                wallets[side] = json.loads(row[0])
                if dec(wallets[side]["wallet"]) != snapshots[side].wallet:
                    raise TradingError("模拟账户余额已变化，等待新快照")
            for side, member in members.items():
                change = -amount if side == pending["source"] else amount
                wallets[side]["wallet"] = wire(dec(wallets[side]["wallet"]) + change)
                db.execute("UPDATE kv SET data=? WHERE key=?", (json.dumps(wallets[side]), "paper:" + member["id"]))
            db.execute("INSERT INTO kv(key,data) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
                       (key, json.dumps(state)))
        for member in members.values():
            self.engine.broker(member).reload()

    def tick(self, pair, snapshots, pending_orders=False, *, snapshot_guards=None, snapshot_loader=None):
        """Return display state; the caller must obey ``blocks_trading``."""
        config = validate_margin(pair.get("margin", {}))
        try:
            state = self.store.get("pair_margin:" + pair["id"], {})
        except Exception:
            return self._view({}, config, "blocked", "保证金划转日志无法读取，停止新开仓和新划转；请检查服务器日志及存储状态", blocks=True)
        if not isinstance(state, dict):
            return self._view({}, config, "blocked", "保证金划转日志无效，停止新开仓和新划转；请人工核对服务器日志与交易所划转记录", blocks=True)
        preparing = False
        try:
            members = self._members(pair)
            now = time.time()
            for field in ("checked_at", "next_check_at", "cooldown_until"):
                if field in state and (type(state[field]) not in (int, float) or not math.isfinite(state[field])):
                    raise TradingError("保证金划转日志时间无效")
            pending = state.get("pending")
            if pending is not None and not self._valid_pending(pending):
                raise TradingError("保证金划转待核对记录无效")
            if pending:
                positive(pending.get("amount"))
                if now < state.get("next_check_at", 0):
                    if self.resume_ready(state) and (not state.get("blocked_reason") or state.get("retry_without_blocking")):
                        return self._view(state, config, "unknown",
                            f"未知划转保留核对，交易按新余额继续检查；转出侧额外预留 {pending['amount']} USD1，暂停新划转")
                    return self._view(state, config, pending.get("status", "unknown"),
                        state.get("blocked_reason") or ("等待下一次划转只读核对" + _remaining_time(state["next_check_at"], now)
                        + "；保留原请求，期间不开始新开仓或新划转"))
                state.update(checked_at=now, next_check_at=now + config["check_interval_seconds"])
                self.store.put("pair_margin:" + pair["id"], state)
                # A changed environment must not query the wrong accounts and
                # accidentally match an unrelated transfer of the same amount.
                self._verify_pending_identity(pair, members, pending)
                if pending["status"] == "acknowledged":
                    return self._refresh_acknowledged(pair, members, state, config)
                return self._reconcile(pair, members, state, config)
            if not config["enabled"]:
                return self._view(state, config, "disabled", "自动保证金平衡未启用")
            if not pair.get("enabled"):
                return self._view(state, config, "paused", "配对组已暂停，不发起新划转；已有划转仍继续只读核对，需启动配对组后才重新评估新划转")
            if any(not self.engine.live_allowed(member) for member in members.values()):
                return self._view(state, config, "blocked", "实盘全局开关未开启，禁止划转", blocks=True)
            if pending_orders:
                return self._view(state, config, "waiting", "配对组正在核对订单或减回本轮仓位，暂不划转；完成后重新评估保证金")
            if state.get("api_notice") and now < state.get("next_check_at", 0):
                return self._view(state, config, "waiting" if state.get("retry_without_blocking") else "blocked", state.get("blocked_reason") or
                    "等待 API 请求预算恢复后重新检查保证金", blocks=not state.get("retry_without_blocking"))
            if now < state.get("cooldown_until", 0):
                return self._view(state, config, "cooldown", "划转冷却中" + _remaining_time(state["cooldown_until"], now)
                                  + "；结束后重新评估划转条件")
            if now < state.get("next_check_at", 0):
                blocked = bool(state.get("blocked_reason") and not state.get("retry_without_blocking"))
                return self._view(state, config, "blocked" if blocked else "waiting",
                    state.get("blocked_reason", "等待下一次保证金检查" + _remaining_time(state["next_check_at"], now)
                              + "；届时重新评估余额差额及安全可划条件"), blocks=blocked)
            state.update(checked_at=now, next_check_at=now + config["check_interval_seconds"])
            self.store.put("pair_margin:" + pair["id"], state)
            preparing = True
            if snapshot_loader is not None:
                snapshots, snapshot_guards = snapshot_loader()
            self._check_snapshots(pair, snapshots)
            diagnostics = {}
            may_transfer = self._may_need_transfer(snapshots, config)
            if may_transfer and members["long"]["mode"] == "live":
                # An optimistic withdrawal ceiling cannot tighten the existing
                # per-transfer cap. This local preview can only decline work;
                # it never authorizes a transfer or substitutes for live limits.
                may_transfer = self._plan(pair, members, snapshots, config,
                    dict.fromkeys(members, config["max_transfer"]), diagnostics=diagnostics) is not None
            if not may_transfer:
                # No extra account read is needed to decline a transfer. Hot
                # snapshots remain protected by the caller's account leases;
                # no result from this precheck can authorize a funds write.
                for side, snapshot in snapshots.items():
                    guard = (snapshot_guards or {}).get(side)
                    if callable(guard):
                        guard()
                    elif members[side]["mode"] == "live" and type(getattr(snapshot, "account_read_generation", None)) is int:
                        self.engine.broker(members[side]).require_snapshot_current(snapshot)
                    snapshot.require_fresh()
                self._record_success(pair, state)
                return self._view(state, config, "waiting", self._no_transfer_reason(snapshots, config, diagnostics))
            if members["long"]["mode"] == "paper":
                diagnostics = {}
                plan = self._plan(pair, members, snapshots, config,
                    {side: max(Decimal(0), min(snapshot.available, snapshot.wallet)) for side, snapshot in snapshots.items()},
                    diagnostics=diagnostics)
                if plan is None:
                    self._record_success(pair, state)
                    return self._view(state, config, "waiting", self._no_transfer_reason(snapshots, config, diagnostics))
                from .pair_recovery import require_archived_orders_clear
                require_archived_orders_clear(self.engine, pair)
                for guard in (snapshot_guards or {}).values():
                    guard()
                pending = {**plan, "request_id": uuid.uuid4().hex, "created_at": now, "status": "submitting"}
                state["cooldown_until"] = now + config["cooldown_seconds"]
                self._paper(pair, members, snapshots, state, pending)
                self._record_success(pair, state)
                return self._view(state, config, "paper_confirmed", "模拟 USD1 划转已原子入账，系统重新读取两侧余额；取得新快照前不开始新开仓或新划转", blocks=True, plan=plan)
            return self._live(pair, members, snapshots, state, config, snapshot_guards)
        except PairRecoveryConflict:
            raise
        except TradingError as exc:
            # Local validation messages are fixed strings; remote error bodies
            # and credential values never enter durable state or the dashboard.
            notice = api_wait_notice(exc)
            detail = self._read_error_detail(exc) if isinstance(exc, ExchangeError) else None
            reason = (notice["text"] if notice else
                f"Aster 划转只读检查失败（{detail}），系统将重试账户或流水查询；核实前不开始新开仓或新划转"
                if isinstance(exc, ExchangeError) else str(exc))
            soft = isinstance(exc, ExchangeError) and (
                preparing and state.get("pending") is None or self.resume_ready(state))
            if soft:
                reason = "本轮保证金检查已跳过，稍后重新读取余额；交易仍按新鲜快照及原风控检查。" + (
                    notice["text"] if notice else detail or str(exc))
            state = self._record_failure(pair, state, reason, exc=exc, soft=soft)
            if (isinstance(exc, ExchangeError) and isinstance(state.get("pending"), dict)
                    and state["pending"].get("status") == "acknowledged"):
                reason = "交易所已确认划转，但两侧余额尚未完成刷新；继续只读重试，不重新划转" + (
                    "；最近反馈：" + (notice["text"] if notice else detail))
            return self._view(state, config, "waiting" if soft and not state.get("pending") else self._failure_status(state),
                              reason, blocks=not soft or not state.get("retry_without_blocking"))
        except Exception:
            state = self._record_failure(pair, state, "保证金日志或账户读取失败，禁止新划转")
            return self._view(state, config, self._failure_status(state), "保证金日志或账户读取失败，禁止新划转", blocks=True)

    @staticmethod
    def _read_error_detail(exc):
        """Keep useful read diagnostics without publishing remote response text."""
        details = []
        if str(exc) == "Aster 网络连接失败":
            details.append("网络连接失败")
        elif str(exc).startswith("Aster 返回无法识别的响应"):
            details.append("响应格式无法识别")
        if type(exc.http_status) is int and 100 <= exc.http_status <= 599:
            details.append(f"HTTP {exc.http_status}")
        if type(exc.code) is int and -1000000 <= exc.code < 0:
            details.append(f"错误码 {exc.code}")
        return "；".join(details) or "未返回可识别的错误类别或错误码"

    @staticmethod
    def _failure_status(state):
        pending = state.get("pending")
        if isinstance(pending, dict) and pending.get("status") == "acknowledged":
            return "acknowledged"
        return "unknown" if pending else "blocked"

    def _record_success(self, pair, state):
        state.pop("api_notice", None)
        state.pop("blocked_reason", None)
        state.pop("retry_without_blocking", None)
        self.store.put("pair_margin:" + pair["id"], state)

    @staticmethod
    def _apply_api_wait(state, exc):
        notice = api_wait_notice(exc)
        if notice:
            state["api_notice"] = notice
        delay = getattr(exc, "retry_after", 0)
        if type(delay) in (int, float) and math.isfinite(delay) and delay > 0:
            deadline = state.get("next_check_at", 0)
            if type(deadline) not in (int, float) or not math.isfinite(deadline):
                deadline = 0
            state["next_check_at"] = max(deadline, time.time() + delay)

    def _record_failure(self, pair, state, reason, *, exc=None, soft=False):
        # Re-read durable state: a rolled-back paper transfer or failed receipt
        # commit must never be saved as completed by an error handler.
        try:
            durable = self.store.get("pair_margin:" + pair["id"], {})
            if isinstance(durable, dict):
                durable["blocked_reason"] = reason
                durable["retry_without_blocking"] = bool(soft and (durable.get("pending") is None or self.resume_ready(durable)))
                self._apply_api_wait(durable, exc)
                self.store.put("pair_margin:" + pair["id"], durable)
                return durable
        except Exception:
            pass
        self._apply_api_wait(state, exc)
        return state

    def _live_snapshots(self, pair, members, snapshots, snapshot_guards=None):
        brokers = {side: self.engine.broker(member) for side, member in members.items()}
        originals = dict(snapshots)
        guards = {}
        replacements = []
        for side, broker in brokers.items():
            if not callable(getattr(broker, "require_snapshot_current", None)) or not hasattr(broker, "_snapshot_lock"):
                raise TradingError("账户缺少可撤销的快照校验，禁止划转")
            # PairTrader can lend its revocable hot lease for this synchronous
            # call under the same account locks. A bare snapshot is insufficient.
            guard = (snapshot_guards or {}).get(side)
            if callable(guard):
                # Keep the caller's configuration/identity guard as well as
                # the snapshot lease, including REST reads with generations.
                guards[side] = guard
            elif type(getattr(originals[side], "account_read_generation", None)) is not int:
                replacements.append(side)
        if replacements:
            # Reuse the ordinary 15s mode cache; account events still invalidate
            # it. Every read gets a new generation-checked balance/position view.
            with ThreadPoolExecutor(max_workers=len(replacements), thread_name_prefix="margin-read") as pool:
                reads = {side: pool.submit(brokers[side].snapshot, [pair.get("symbol", "XAUUSD1")], fresh_modes=False)
                         for side in replacements}
                originals.update({side: future.result() for side, future in reads.items()})
        for side in brokers:
            if side in guards:
                continue
            if type(getattr(originals[side], "account_read_generation", None)) is not int:
                raise TradingError("账户快照缺少有效代次，禁止划转")
            guards[side] = lambda side=side: brokers[side].require_snapshot_current(originals[side])

        def require_current():
            # Check both generations at one local admission boundary. Holding
            # these locks only for validation does not stall account events on
            # network I/O. Keep originals: dataclasses.replace drops the token.
            with ExitStack() as locks:
                for side in sorted(brokers, key=lambda value: members[value]["id"]):
                    locks.enter_context(brokers[side]._snapshot_lock)
                for guard in guards.values():
                    guard()
                if any(not self.engine.live_allowed(member) for member in members.values()):
                    raise TradingError("实盘全局开关已关闭，禁止划转")

        require_current()
        self._check_snapshots(pair, originals)
        return originals, require_current

    @staticmethod
    def _newer_occupied(snapshot, payload, asset):
        rows = LiveBroker._position_rows(payload.get("positions"))
        for row in rows.values():
            leverage = dec(row.get("leverage"))
            if leverage != leverage.to_integral_value() or not 1 <= leverage <= 125 or type(row.get("isolated")) is not bool:
                raise TradingError("账户持仓杠杆或保证金模式字段无效，禁止划转")
        positions = {(position.symbol, position.side): position for position in snapshot.positions}
        if len(positions) != len(snapshot.positions):
            raise TradingError("账户快照包含重复持仓，禁止划转")
        if any(dec(row["positionAmt"]) and key not in positions for key, row in rows.items()):
            raise TradingError("账户持仓在划转检查期间变化，等待新快照")
        occupied = Fraction(0)
        for key, position in positions.items():
            row = rows.get(key)
            if row is None:
                if position.qty:
                    raise TradingError("账户持仓在划转检查期间变化，等待新快照")
                continue
            if (dec(row["positionAmt"]).copy_abs() != position.qty
                    or dec(row.get("leverage")) != position.leverage
                    or type(row.get("isolated")) is not bool or row["isolated"] != position.isolated):
                raise TradingError("账户持仓、杠杆或保证金模式在划转检查期间变化，等待新快照")
            margin = position.occupied_margin_exact
            if "markPrice" in row:
                margin = max(margin, Fraction(position.qty) * Fraction(positive(row["markPrice"], True)) / position.leverage)
            for field in ("initialMargin", "positionInitialMargin"):
                if field in row:
                    margin = max(margin, Fraction(positive(row[field], True)))
            occupied += margin
        # Newer exchange-reported occupation may rise without a user event
        # (mark price movement). It must never increase transferable risk room.
        for field in ("initialMargin", "positionInitialMargin"):
            if field in asset:
                occupied = max(occupied, Fraction(positive(asset[field], True)))
        return occupied

    def _live(self, pair, members, snapshots, state, config, snapshot_guards=None):
        # New transfers, including archived-order guards whose query method
        # requests recovery priority, may never spend the recovery reserve.
        with ExitStack() as ordinary:
            seen = set()
            for member in members.values():
                budget = getattr(getattr(self.engine.broker(member), "api", None), "budget", None)
                if budget is not None and id(budget) not in seen:
                    ordinary.enter_context(budget.cycle_accounting())
                    seen.add(id(budget))
            self._require_budget(members, self._live_budget_weights(pair, members, snapshots, snapshot_guards))
            result = self._submit_live(pair, members, snapshots, state, config, snapshot_guards)
        # Only an already accepted write may use the recovery reserve. Leave
        # the ordinary context before its one confirmation refresh.
        if isinstance(state.get("pending"), dict) and state["pending"].get("status") == "acknowledged":
            return self._refresh_acknowledged(pair, members, state, config)
        return result

    def _submit_live(self, pair, members, snapshots, state, config, snapshot_guards=None):
        key = "pair_margin:" + pair["id"]
        snapshots, require_current = self._live_snapshots(pair, members, snapshots, snapshot_guards)
        with self._master(pair, members) as (api, master, children):
            listing = api.call("GET", "/fapi/v3/getSubAccountList", signed=True, weight=5)
            accounts = self._account_rows(members)
            self._verify(listing, children, accounts)
            available, occupied, wallets = {}, {}, {}
            for side, payload in accounts.items():
                rows = payload.get("assets")
                assets = [row for row in rows if isinstance(row, dict) and row.get("asset") == "USD1"] if isinstance(rows, list) else []
                if len(assets) != 1 or payload.get("canTrade") is not True:
                    raise TradingError("账户缺少唯一的 USD1 资产或交易权限")
                asset = assets[0]
                # ACCOUNT_UPDATE.wb is total wallet balance, not available or
                # cross-wallet balance. Missing/invalid REST values disable
                # this optional evidence path without inventing a baseline.
                try:
                    wallets[side] = dec(asset.get("walletBalance"))
                except TradingError:
                    pass
                available[side] = positive(asset.get("maxWithdrawAmount"), True)
                occupied[side] = self._newer_occupied(snapshots[side], payload, asset)
                # Prices can move between two reads. Use the newer cash balance
                # and the LOWER equity / HIGHER maintenance, never demand exact
                # equality that would prevent transfers in a moving market.
                equity = (dec(asset["marginBalance"]) if "marginBalance" in asset else
                          dec(asset.get("crossWalletBalance")) + dec(asset.get("crossUnPnl")))
                snapshots = {**snapshots, side: replace(snapshots[side], available=dec(asset.get("availableBalance")),
                    equity=min(snapshots[side].equity, equity),
                    maintenance=max(snapshots[side].maintenance, positive(asset.get("maintMargin"), True)))}
            self._check_snapshots(pair, snapshots)
            require_current()
            diagnostics = {}
            plan = self._plan(pair, members, snapshots, config, available,
                occupied_floors=occupied, diagnostics=diagnostics)
            if plan is None:
                self._record_success(pair, state)
                return self._view(state, config, "waiting", self._no_transfer_reason(snapshots, config, diagnostics))
            before_at = time.time()
            before = self._income(members, int((before_at - 90) * 1000), int(before_at * 1000))
            # This pair submits market orders. Pending batches are gated by
            # PairTrader; archived orders are checked by their original IDs.
            # Fresh account availability/withdrawal limits include reserved funds.
            from .pair_recovery import require_archived_orders_clear
            require_archived_orders_clear(self.engine, pair)
            require_current()
            created = time.time()
            pending = {**plan, "request_id": uuid.uuid4().hex, "created_at": created, "status": "submitting",
                "identity": self._fingerprint(pair, members, children, master),
                "before_receipts": {side: [[row["incomeType"], _identifier(row.get("tranId"))] for row in rows
                    if row.get("incomeType") in TRANSFER_TYPES and _identifier(row.get("tranId"))] for side, rows in before.items()}}
            checkpoints = {}
            for side, wallet in wallets.items():
                checkpoint = getattr(self.engine.broker(members[side]), "transfer_ws_checkpoint", None)
                value = checkpoint(wallet) if callable(checkpoint) else None
                if value is not None:
                    checkpoints[side] = value
            if set(checkpoints) == set(members):
                pending["ws_checkpoints"] = checkpoints
            state.update(pending=pending, last_transfer=pending, cooldown_until=created + config["cooldown_seconds"])
            state.pop("api_notice", None)
            state.pop("blocked_reason", None)
            self.store.put(key, state)  # Must commit before any signed mutation.
            def before_submit():
                # Validate borrowed leases after signing, then revoke them just
                # before transport. Earlier revocation would reject our own read.
                require_current()
                for member in members.values():
                    self.engine.broker(member).discard_cycle_hot_snapshot("子账户保证金划转，重新读取余额")

            transfer = TransferAPI(master, budget=api.budget, before_submit=before_submit)
            try:
                response = transfer.call("POST", "/fapi/v3/subAccountTransfer", {
                    "toAccountAddress": children[plan["destination"]]["user"], "asset": "USD1", "amount": plan["amount"],
                    "kindType": "FUTURE_FUTURE", "fromAccountAddress": children[plan["source"]]["user"]}, signed=True, weight=5)
                if (not isinstance(response, dict) or type(response.get("code")) not in (int, str)
                        or response.get("code") not in (0, 200, "0", "200")
                        or not isinstance(response.get("msg"), str) or response["msg"].strip().lower() != "success"):
                    raise AmbiguousOrder("划转没有明确的接受回执")
                pending.update(status="acknowledged", acknowledged_at=time.time(), acknowledgement_code=int(response["code"]))
                if _identifier(response.get("tranId")):
                    pending["transaction_id"] = _identifier(response["tranId"])
            except Exception as exc:
                rejected = isinstance(exc, RequestNotSent) or (isinstance(exc, ExchangeError)
                    and not isinstance(exc, AmbiguousOrder) and exc.code in REJECTION_CODES)
                pending["status"] = "rejected" if rejected else "unknown"
                if rejected:
                    state["pending"] = None
                    state["retry_without_blocking"] = True
                self._apply_api_wait(state, exc)
                if state.get("api_notice"):
                    state["blocked_reason"] = state["api_notice"]["text"]
            finally:
                transfer.close()
                self._invalidate(members)
            self.store.put(key, state)
            if pending["status"] == "acknowledged":
                return self._view(state, config, "acknowledged", "交易所已确认划转，继续刷新两侧余额", blocks=True)
            reason = {"unknown": "划转结果未知，停止新划转和新开仓；保留原记录供只读核对，不重发原请求",
                      "rejected": "划转请求明确未被接受，本次未划转；冷却结束后系统重新评估划转条件"}[pending["status"]]
            if state.get("api_notice"):
                reason += "；" + state["api_notice"]["text"]
            return self._view(state, config, pending["status"], reason, blocks=True, plan=plan)
