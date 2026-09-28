"""USD1 transfers between verified subaccounts; never retry an uncertain write.

The caller holds the pair lock and both member-account locks. Transfer identity
comes from authenticated Aster responses, never a locally supplied account name.
"""
from contextlib import ExitStack, contextmanager
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

from .exchange import API, AmbiguousOrder, ExchangeError, LiveBroker, RequestNotSent, credentials_for
from .models import TradingError, dec, positive, wire


DEFAULT_MARGIN = {"enabled": False, "master_env_prefix": "", "check_interval_seconds": 5,
    "threshold": "10", "min_transfer": "1", "max_transfer": "1000", "buffer_ratio": "0.05",
    "cooldown_seconds": 30}
TRANSFER_TYPES = frozenset({"TRANSFER", "SUBUSER_ASSET_TRANSFER"})
TRANSFER_UNIT = Fraction(Decimal("0.00000001"))
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
        "status", "confirmed_at", "acknowledged_at", "refreshed_at", "transaction_id", "income_type") if key in record}


class MarginBalancer:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def _members(self, pair):
        members = {side: self.store.account(pair[side + "_account_id"]) for side in ("long", "short")}
        if not all(members.values()) or members["long"]["id"] == members["short"]["id"]:
            raise TradingError("保证金平衡需要两个存在且不同的子账户")
        if len({member["mode"] for member in members.values()}) != 1:
            raise TradingError("模拟与实盘账户不能互相划转")
        return members

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
    def _plan(pair, members, snapshots, config, withdrawable, *, occupied_floors=None):
        delta = MarginBalancer._balance_delta(snapshots)
        if abs(delta) <= dec(config["threshold"]):
            return None
        source, destination = ("long", "short") if delta > 0 else ("short", "long")
        snapshot = snapshots[source]
        buffer = Fraction(dec(config["buffer_ratio"]))
        limits = [positive(members[source].get("policy", {}).get("margin_limit", "0.5"))]
        if "margin_limit" in pair.get("ordinary", {}):
            limits.append(positive(pair["ordinary"]["margin_limit"]))
        limit = Fraction(min(limits)) - buffer
        if limit <= 0 or limit > 1:
            raise TradingError("风险缓冲必须小于来源账户保证金占用上限")
        equity = Fraction(positive(snapshot.equity))
        # Keep both spare cash and enough post-transfer equity for ALL occupied
        # margin. Available funds are not synonymous with safely transferable funds.
        occupied = max(snapshot.occupied_margin_exact, (occupied_floors or {}).get(source, Fraction(0)))
        risk_room = equity - max(occupied, Fraction(snapshot.maintenance)) / limit
        cash_room = Fraction(snapshot.available) - equity * buffer
        amount = min(Fraction(abs(delta)) / 2, Fraction(dec(config["max_transfer"])),
            Fraction(positive(withdrawable[source], True)), cash_room, risk_room)
        amount = max(Fraction(0), amount)
        units = amount // TRANSFER_UNIT
        rounded = Decimal(int(units)) * Decimal("0.00000001")
        if rounded < dec(config["min_transfer"]):
            return None
        return {"source": source, "destination": destination, "amount": wire(rounded)}

    @staticmethod
    def _view(state, config, status, reason, *, blocks=False, plan=None):
        return {"enabled": config["enabled"], "status": status, "reason": reason,
            "blocks_trading": bool(blocks or state.get("pending")), "checked_at": state.get("checked_at"),
            "pending": _public_record(state.get("pending")), "last_transfer": _public_record(state.get("last_transfer")),
            "plan": plan, "next_check_at": state.get("next_check_at", 0), "cooldown_until": state.get("cooldown_until", 0)}

    @staticmethod
    def status_view(pair, journal, runtime_view=None):
        """Project the durable journal without letting an older view hide a write."""
        config = validate_margin(pair.get("margin", {}))
        if journal is None:
            journal = {}
        if not isinstance(journal, dict):
            return MarginBalancer._view({}, config, "blocked", "保证金划转日志无效，需要核对", blocks=True)
        view = lambda status, reason, **kwargs: MarginBalancer._view(journal, config, status, reason, **kwargs)
        pending = journal.get("pending")
        if pending is not None:
            if not isinstance(pending, dict) or pending.get("status") not in {"submitting", "unknown", "accepted", "acknowledged"}:
                return view("blocked", "保证金划转待核对记录无效", blocks=True)
            status = "unknown" if pending["status"] == "submitting" else pending["status"]
            reasons = {"unknown": "划转结果未知，保留记录并暂停新交易，不会重发",
                "accepted": "等待两侧 USD1 划转流水核实", "acknowledged": "交易所已确认划转，等待两侧余额刷新"}
            return view(status, journal.get("blocked_reason") or reasons[status], blocks=True)
        if not config["enabled"]:
            return view("disabled", "自动保证金平衡未启用")
        if not pair.get("enabled"):
            return view("paused", "配对组已暂停，只核对已有划转")
        if journal.get("blocked_reason"):
            return view("blocked", journal["blocked_reason"], blocks=True)
        for field in ("checked_at", "next_check_at", "cooldown_until"):
            value = journal.get(field)
            if field in journal and (type(value) not in (int, float) or not math.isfinite(value)):
                return view("blocked", "保证金划转日志时间无效", blocks=True)
        if time.time() < journal.get("cooldown_until", 0):
            return view("cooldown", "划转冷却中")
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
        return view("waiting", "等待保证金检查")

    def _income(self, members, start, end):
        result = {}
        for side, member in members.items():
            rows = self.engine.broker(member).api.call("GET", "/fapi/v3/income",
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
            return self._view(state, config, "unknown", "划转结果未知且没有交易编号；保留记录并暂停新交易，不会按余额猜测或重发")
        start = int(pending["created_at"] * 1000) // 1000 * 1000
        end = int((pending["created_at"] + 90) * 1000)
        rows = self._income(members, start, end)
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
            return self._view(state, config, pending["status"], "等待两侧 USD1 同交易编号、金额和时间一致的划转流水")
        kind, txn = common.pop()
        done = {**pending, "status": "confirmed", "confirmed_at": time.time(), "transaction_id": txn, "income_type": kind}
        state.update(pending=None, last_transfer=done)
        state["used_receipts"] = (state.get("used_receipts", []) + [[kind, txn]])[-100:]
        self.store.put("pair_margin:" + pair["id"], state)
        self._invalidate(members)
        return self._view(state, config, "confirmed", "划转已由两侧流水核实，等待新账户快照", blocks=True)

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
        for member in members.values():
            current = self.engine.broker(member).snapshot([pair.get("symbol", "XAUUSD1")], fresh_modes=True)
            current.require_fresh()
            if current.timestamp < acknowledged_at:
                raise TradingError("划转后的账户查询返回旧快照，继续等待新余额")
        done = {**pending, "refreshed_at": time.time()}
        state.update(pending=None, last_transfer=done)
        self.store.put("pair_margin:" + pair["id"], state)
        self._invalidate(members)
        return self._view(state, config, "acknowledged", "交易所回执确认，余额已刷新；等待下一轮账户快照", blocks=True,
            plan={key: done[key] for key in ("source", "destination", "amount")})

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

    def tick(self, pair, snapshots, pending_orders=False):
        """Return display state; the caller must obey ``blocks_trading``."""
        config = validate_margin(pair.get("margin", {}))
        try:
            state = self.store.get("pair_margin:" + pair["id"], {})
        except Exception:
            return self._view({}, config, "blocked", "保证金划转日志无法读取，禁止新交易", blocks=True)
        if not isinstance(state, dict):
            return self._view({}, config, "blocked", "保证金划转日志无效，需要核对", blocks=True)
        try:
            members = self._members(pair)
            now = time.time()
            for field in ("checked_at", "next_check_at", "cooldown_until"):
                if field in state and (type(state[field]) not in (int, float) or not math.isfinite(state[field])):
                    raise TradingError("保证金划转日志时间无效")
            pending = state.get("pending")
            if pending is not None and (not isinstance(pending, dict) or not pending
                    or pending.get("status") not in {"submitting", "acknowledged", "accepted", "unknown"}
                    or {pending.get("source"), pending.get("destination")} != {"long", "short"}
                    or not _identifier(pending.get("request_id"))
                    or not isinstance(pending.get("identity"), str)
                    or type(pending.get("created_at")) not in (int, float)
                    or not math.isfinite(pending["created_at"]) or pending["created_at"] <= 0):
                raise TradingError("保证金划转待核对记录无效")
            if pending:
                positive(pending.get("amount"))
                if now < state.get("next_check_at", 0):
                    return self._view(state, config, pending.get("status", "unknown"), "等待划转核对间隔")
                state.update(checked_at=now, next_check_at=now + config["check_interval_seconds"])
                self.store.put("pair_margin:" + pair["id"], state)
                if members["long"]["mode"] != "live":
                    raise TradingError("模拟划转出现未完成日志，需要核对")
                # A changed environment must not query the wrong accounts and
                # accidentally match an unrelated transfer of the same amount.
                with self._master(pair, members) as (_, master, children):
                    if pending.get("identity") != self._fingerprint(pair, members, children, master):
                        raise TradingError("划转待核对记录与当前账户身份不一致")
                if pending["status"] == "acknowledged":
                    return self._refresh_acknowledged(pair, members, state, config)
                return self._reconcile(pair, members, state, config)
            if not config["enabled"]:
                return self._view(state, config, "disabled", "自动保证金平衡未启用")
            if not pair.get("enabled"):
                return self._view(state, config, "paused", "配对组已暂停，只核对已有划转")
            if any(not self.engine.live_allowed(member) for member in members.values()):
                return self._view(state, config, "blocked", "实盘全局开关未开启，禁止划转", blocks=True)
            if pending_orders:
                return self._view(state, config, "waiting", "先核对配对委托，再评估保证金划转")
            if now < state.get("cooldown_until", 0):
                return self._view(state, config, "cooldown", "划转冷却中")
            if now < state.get("next_check_at", 0):
                return self._view(state, config, "blocked" if state.get("blocked_reason") else "waiting",
                    state.get("blocked_reason", "等待保证金检查间隔"), blocks=bool(state.get("blocked_reason")))
            state.update(checked_at=now, next_check_at=now + config["check_interval_seconds"])
            state.pop("blocked_reason", None)
            self.store.put("pair_margin:" + pair["id"], state)
            self._check_snapshots(pair, snapshots)
            if not self._may_need_transfer(snapshots, config):
                # No extra account read is needed to decline a transfer. Hot
                # snapshots remain protected by the caller's account leases;
                # no result from this precheck can authorize a funds write.
                for side, snapshot in snapshots.items():
                    if members[side]["mode"] == "live" and type(getattr(snapshot, "account_read_generation", None)) is int:
                        self.engine.broker(members[side]).require_snapshot_current(snapshot)
                    snapshot.require_fresh()
                return self._view(state, config, "waiting", "余额差未达到阈值，或可平衡金额小于最小划转额")
            if members["long"]["mode"] == "paper":
                plan = self._plan(pair, members, snapshots, config,
                    {side: max(Decimal(0), min(snapshot.available, snapshot.wallet)) for side, snapshot in snapshots.items()})
                if plan is None:
                    self.store.put("pair_margin:" + pair["id"], state)
                    return self._view(state, config, "waiting", "余额差未达到阈值，或来源账户没有安全可划金额")
                pending = {**plan, "request_id": uuid.uuid4().hex, "created_at": now, "status": "submitting"}
                state["cooldown_until"] = now + config["cooldown_seconds"]
                self._paper(pair, members, snapshots, state, pending)
                return self._view(state, config, "paper_confirmed", "模拟 USD1 划转已原子入账，等待新快照", blocks=True, plan=plan)
            return self._live(pair, members, snapshots, state, config)
        except TradingError as exc:
            # Local validation messages are fixed strings; remote error bodies
            # and credential values never enter durable state or the dashboard.
            reason = "Aster 划转只读检查失败，等待重试" if isinstance(exc, ExchangeError) else str(exc)
            state = self._record_failure(pair, state, reason)
            if (isinstance(exc, ExchangeError) and isinstance(state.get("pending"), dict)
                    and state["pending"].get("status") == "acknowledged"):
                reason = "交易所已确认划转，但两侧余额尚未完成刷新；继续只读重试，不重新划转"
            return self._view(state, config, self._failure_status(state), reason, blocks=True)
        except Exception:
            state = self._record_failure(pair, state, "保证金日志或账户读取失败，禁止新划转")
            return self._view(state, config, self._failure_status(state), "保证金日志或账户读取失败，禁止新划转", blocks=True)

    @staticmethod
    def _failure_status(state):
        pending = state.get("pending")
        if isinstance(pending, dict) and pending.get("status") == "acknowledged":
            return "acknowledged"
        return "unknown" if pending else "blocked"

    def _record_failure(self, pair, state, reason):
        # Re-read durable state: a rolled-back paper transfer or failed receipt
        # commit must never be saved as completed by an error handler.
        try:
            durable = self.store.get("pair_margin:" + pair["id"], {})
            if isinstance(durable, dict):
                durable["blocked_reason"] = reason
                self.store.put("pair_margin:" + pair["id"], durable)
                return durable
        except Exception:
            pass
        return state

    def _live_snapshots(self, pair, members, snapshots):
        brokers = {side: self.engine.broker(member) for side, member in members.items()}
        originals = dict(snapshots)
        replacements = []
        for side, broker in brokers.items():
            if not callable(getattr(broker, "require_snapshot_current", None)) or not hasattr(broker, "_snapshot_lock"):
                raise TradingError("账户缺少可撤销的快照校验，禁止划转")
            # Hot snapshots carry a lease held by PairTrader, not an ordinary
            # generation token. Obtain our own revocable read before planning.
            if type(getattr(originals[side], "account_read_generation", None)) is not int:
                replacements.append(side)
        if replacements:
            # Reuse the ordinary 15s mode cache; account events still invalidate
            # it. Every read gets a new generation-checked balance/position view.
            with ThreadPoolExecutor(max_workers=len(replacements), thread_name_prefix="margin-read") as pool:
                reads = {side: pool.submit(brokers[side].snapshot, [pair.get("symbol", "XAUUSD1")], fresh_modes=False)
                         for side in replacements}
                originals.update({side: future.result() for side, future in reads.items()})
        for side in brokers:
            if type(getattr(originals[side], "account_read_generation", None)) is not int:
                raise TradingError("账户快照缺少有效代次，禁止划转")

        def require_current():
            # Check both generations at one local admission boundary. Holding
            # these locks only for validation does not stall account events on
            # network I/O. Keep originals: dataclasses.replace drops the token.
            with ExitStack() as locks:
                for side in sorted(brokers, key=lambda value: members[value]["id"]):
                    locks.enter_context(brokers[side]._snapshot_lock)
                for side, broker in brokers.items():
                    broker.require_snapshot_current(originals[side])
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

    def _live(self, pair, members, snapshots, state, config):
        key = "pair_margin:" + pair["id"]
        snapshots, require_current = self._live_snapshots(pair, members, snapshots)
        with self._master(pair, members) as (api, master, children):
            listing = api.call("GET", "/fapi/v3/getSubAccountList", signed=True, weight=5)
            accounts = self._account_rows(members)
            self._verify(listing, children, accounts)
            available, occupied = {}, {}
            for side, payload in accounts.items():
                rows = payload.get("assets")
                assets = [row for row in rows if isinstance(row, dict) and row.get("asset") == "USD1"] if isinstance(rows, list) else []
                if len(assets) != 1 or payload.get("canTrade") is not True:
                    raise TradingError("账户缺少唯一的 USD1 资产或交易权限")
                asset = assets[0]
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
            plan = self._plan(pair, members, snapshots, config, available, occupied_floors=occupied)
            if plan is None:
                self.store.put(key, state)
                return self._view(state, config, "waiting", "余额差未达到阈值，或来源账户没有安全可划金额")
            before_at = time.time()
            before = self._income(members, int((before_at - 90) * 1000), int(before_at * 1000))
            for side, member in members.items():
                orders = self.engine.broker(member).api.call("GET", "/fapi/v3/openOrders", signed=True, weight=40)
                if not isinstance(orders, list):
                    raise TradingError("账户未完成委托响应无效，禁止划转")
                snapshots = {**snapshots, side: replace(snapshots[side], open_orders=orders)}
            self._check_snapshots(pair, snapshots, require_orders=True)
            require_current()
            created = time.time()
            pending = {**plan, "request_id": uuid.uuid4().hex, "created_at": created, "status": "submitting",
                "identity": self._fingerprint(pair, members, children, master),
                "before_receipts": {side: [[row["incomeType"], _identifier(row.get("tranId"))] for row in rows
                    if row.get("incomeType") in TRANSFER_TYPES and _identifier(row.get("tranId"))] for side, rows in before.items()}}
            state.update(pending=pending, last_transfer=pending, cooldown_until=created + config["cooldown_seconds"])
            self.store.put(key, state)  # Must commit before any signed mutation.
            transfer = TransferAPI(master, budget=api.budget, before_submit=require_current)
            try:
                # Revoke published hot leases now, but do not revoke the very
                # ordinary generations needed for the final pre-send check.
                for member in members.values():
                    self.engine.broker(member).discard_cycle_hot_snapshot("子账户保证金划转，重新读取余额")
                response = transfer.call("POST", "/fapi/v3/subAccountTransfer", {
                    "toAccountAddress": children[plan["destination"]]["user"], "asset": "USD1", "amount": plan["amount"],
                    "kindType": "FUTURE_FUTURE", "fromAccountAddress": children[plan["source"]]["user"]}, signed=True, weight=5)
                if (not isinstance(response, dict) or type(response.get("code")) not in (int, str)
                        or response.get("code") not in (0, 200, "0", "200")
                        or not isinstance(response.get("msg"), str) or response["msg"].strip().lower() != "success"):
                    raise AmbiguousOrder("划转没有明确的接受回执")
                pending.update(status="acknowledged", acknowledged_at=time.time())
                if _identifier(response.get("tranId")):
                    pending["transaction_id"] = _identifier(response["tranId"])
            except Exception as exc:
                rejected = isinstance(exc, RequestNotSent) or (isinstance(exc, ExchangeError)
                    and not isinstance(exc, AmbiguousOrder) and exc.code in REJECTION_CODES)
                pending["status"] = "rejected" if rejected else "unknown"
                if rejected:
                    state["pending"] = None
            finally:
                transfer.close()
                self._invalidate(members)
            self.store.put(key, state)
            if pending["status"] == "acknowledged":
                return self._refresh_acknowledged(pair, members, state, config)
            reason = {"unknown": "划转结果未知，停止后续划转及新交易，保留只读核对",
                      "rejected": "划转请求明确未被接受，冷却后重新评估"}[pending["status"]]
            return self._view(state, config, pending["status"], reason, blocks=True, plan=plan)
