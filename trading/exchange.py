"""Aster V3 EIP-712 API adapter. No automatic retries of signed mutations."""
from dataclasses import asdict
import json
import os
import re
import threading
import time
from urllib.parse import urlencode

import httpx
from eth_account import Account as EthAccount
from eth_account.messages import encode_typed_data

import monitor
from .models import AccountSnapshot, Book, Position, Rules, TradingError, dec, positive, require_non_decreasing_leverage, validate_brackets, wire

BASE = "https://fapi.asterdex.com"


class ExchangeError(TradingError):
    def __init__(self, message, code=None, retry_after=0):
        super().__init__(message)
        self.code, self.retry_after = code, retry_after


class AmbiguousOrder(ExchangeError):
    """The exchange might have accepted a mutation; query before deciding."""


class RateBudget:
    def __init__(self):
        self.lock = threading.Lock()
        self.until, self.window, self.weight = 0.0, time.monotonic(), 0
        self.limit = 1800

    def reserve(self, weight):
        with self.lock:
            now = time.monotonic()
            if now < self.until:
                raise ExchangeError("接口退避中", retry_after=self.until - now)
            if now - self.window >= 60:
                self.window, self.weight = now, 0
            if self.weight + weight > self.limit:
                raise ExchangeError("本地请求预算已用完", retry_after=60 - (now - self.window))
            self.weight += weight

    def block(self, seconds):
        with self.lock:
            self.until = max(self.until, time.monotonic() + seconds)


BUDGET = RateBudget()


class API:
    def __init__(self, credentials=None, transport=None, budget=None):
        self.credentials = credentials
        self.http = httpx.Client(timeout=httpx.Timeout(8, connect=4), follow_redirects=False, transport=transport)
        self.budget = budget or BUDGET
        self.nonce_lock = threading.Lock()
        self.last_nonce = 0

    def signed_parameters(self, params):
        if not self.credentials:
            raise TradingError("账户凭据尚未配置")
        with self.nonce_lock:
            self.last_nonce = max(time.time_ns() // 1000, self.last_nonce + 1)
            nonce = self.last_nonce
        data = {**params, "nonce": str(nonce), "user": self.credentials["user"], "signer": self.credentials["signer"]}
        message = encode_typed_data(full_message={
            "types": {"EIP712Domain": [{"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                         {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                      "Message": [{"name": "msg", "type": "string"}]},
            "primaryType": "Message", "domain": {"name": "AsterSignTransaction", "version": "1", "chainId": 1666,
                         "verifyingContract": "0x0000000000000000000000000000000000000000"},
            "message": {"msg": urlencode(data)},
        })
        signature = EthAccount.sign_message(message, self.credentials["private_key"]).signature.hex()
        data["signature"] = signature if signature.startswith("0x") else "0x" + signature
        return data

    def call(self, method, path, params=None, signed=False, weight=1):
        self.budget.reserve(weight)
        params = self.signed_parameters(params or {}) if signed else (params or {})
        is_write = signed and method != "GET"
        try:
            response = self.http.request(method, BASE + path,
                params=params if method == "GET" else None,
                content=urlencode(params) if method != "GET" else None,
                headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": "AsterAccountDesk/1.0"})
        except httpx.HTTPError:
            error = AmbiguousOrder if is_write else ExchangeError
            raise error("请求结果未知，需核对订单" if is_write else "Aster 网络连接失败") from None
        if response.status_code in (429, 418):
            delay = 180 if response.status_code == 429 else 86400
            try:
                delay = max(delay, float(response.headers.get("Retry-After", "0")))
            except ValueError:
                pass
            self.budget.block(delay)
            raise ExchangeError("Aster 接口限流", retry_after=delay)
        if response.status_code >= 500 and is_write:
            raise AmbiguousOrder("Aster 未确认请求结果，需核对订单")
        try:
            data = response.json()
        except ValueError:
            error = AmbiguousOrder if is_write else ExchangeError
            raise error("Aster 返回无法识别的响应") from None
        code = data.get("code") if isinstance(data, dict) else None
        if isinstance(code, int) and code < 0:
            if code in (-1003, -1015):
                self.budget.block(180)
                raise ExchangeError("Aster 请求或订单限流", code, retry_after=180)
            if code in (-1006, -1007) and is_write:
                raise AmbiguousOrder("Aster 请求超时，需核对订单", code)
            raise ExchangeError(f"Aster 拒绝请求（代码 {code}）", code)
        if not response.is_success:
            raise ExchangeError(f"Aster HTTP {response.status_code}", code)
        return data

    def close(self):
        self.http.close()


def credentials_for(prefix):
    if not re.fullmatch(r"[A-Z][A-Z0-9_]{1,40}", prefix):
        raise TradingError("凭据变量前缀无效")
    values = {key: os.environ.get(prefix + suffix, "") for key, suffix in (
        ("user", "_USER"), ("signer", "_SIGNER"), ("private_key", "_PRIVATE_KEY"))}
    if not all(values.values()):
        raise TradingError("服务器尚未配置该子账户的 API 凭据")
    if any(not re.fullmatch(r"0x[0-9a-fA-F]{40}", values[k]) for k in ("user", "signer")):
        raise TradingError("API 用户或签名地址无效")
    try:
        if EthAccount.from_key(values["private_key"]).address.lower() != values["signer"].lower():
            raise ValueError()
    except (ValueError, TypeError):
        raise TradingError("API 签名密钥与 signer 不匹配") from None
    return values


class MarketData:
    def __init__(self, api=None):
        self.api = api or API()
        self.rules = {}
        self.assets = {}

    def load_rules(self):
        data = self.api.call("GET", "/fapi/v3/exchangeInfo")
        if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
            raise TradingError("交易规则响应无效")
        rules, assets = {}, {}
        for row in data["symbols"]:
            assets[row["symbol"]] = row["marginAsset"]
            if row.get("status") != "TRADING":
                continue
            filters = {f["filterType"]: f for f in row["filters"]}
            lot, price = filters.get("LOT_SIZE"), filters.get("PRICE_FILTER")
            notional = filters.get("MIN_NOTIONAL", filters.get("NOTIONAL", {}))
            if lot and price and ("notional" in notional or "minNotional" in notional):
                rules[row["symbol"]] = Rules(row["symbol"], positive(lot["stepSize"]), positive(price["tickSize"]),
                    positive(lot["minQty"]), positive(lot["maxQty"]), positive(notional.get("notional", notional.get("minNotional"))), row["marginAsset"])
        self.rules, self.assets = rules, assets
        for rate in data.get("rateLimits", []):
            if rate.get("rateLimitType") == "REQUEST_WEIGHT" and rate.get("interval") == "MINUTE" and rate.get("intervalNum") == 1:
                self.api.budget.limit = min(1800, max(1, int(rate["limit"] * .8)))

    def book(self, symbol):
        # Fetch mark first so the executable BBO is as recent as possible.
        mark = self.api.call("GET", "/fapi/v3/premiumIndex", {"symbol": symbol})
        row = self.api.call("GET", "/fapi/v3/ticker/bookTicker", {"symbol": symbol}, weight=2)
        if row.get("symbol") != symbol or mark.get("symbol") != symbol:
            raise TradingError("报价交易代码不匹配")
        book = Book(positive(row["bidPrice"]), positive(row["askPrice"]), positive(row["bidQty"]),
                    positive(row["askQty"]), positive(mark["markPrice"]), min(float(row["time"]), float(mark["time"])) / 1000)
        book.require_fresh()
        return book

    def capacities(self, symbol, leverages):
        self.api.budget.reserve(2)
        try:
            result = monitor.sample({"symbol": symbol, "leverages": sorted(set(leverages)), "timeout_seconds": 8})
            return {v: positive(row["value"], True) for v, row in result.items() if not isinstance(row, Exception)}
        except monitor.MonitorError as exc:
            if exc.retry_after:
                self.api.budget.block(exc.retry_after)
            raise ExchangeError(str(exc), retry_after=exc.retry_after) from None


class LiveBroker:
    mode = "live"

    def __init__(self, credentials, market, api=None):
        self.api = api or API(credentials)
        self.market = market
        self.cached, self.cached_at = {}, {}

    def cached_call(self, key, path, params=None, ttl=300, weight=1):
        if time.monotonic() - self.cached_at.get(key, -1e9) >= ttl:
            self.cached[key] = self.api.call("GET", path, params, signed=True, weight=weight)
            self.cached_at[key] = time.monotonic()
        return self.cached[key]

    def snapshot(self, symbols, fresh_modes=False):
        started = time.time()
        if fresh_modes:
            self.cached_at.pop("dual", None)
            self.cached_at.pop("multi", None)
        dual = self.cached_call("dual", "/fapi/v3/positionSide/dual", ttl=15, weight=30)
        multi = self.cached_call("multi", "/fapi/v3/multiAssetsMargin", ttl=15, weight=30)
        if type(dual.get("dualSidePosition")) is not bool or type(multi.get("multiAssetsMargin")) is not bool:
            raise TradingError("账户持仓或保证金模式响应无效")
        account = self.api.call("GET", "/fapi/v3/accountWithJoinMargin", signed=True, weight=5)
        rows = self.api.call("GET", "/fapi/v3/positionRisk", signed=True, weight=5)
        orders = self.api.call("GET", "/fapi/v3/openOrders", signed=True, weight=40)
        asset = next((a for a in account["assets"] if a["asset"] == "USD1"), None)
        if not asset:
            raise TradingError("账户缺少 USD1 保证金资产")
        if not isinstance(rows, list) or not isinstance(orders, list):
            raise TradingError("持仓或挂单响应无效")
        # Some V3 responses omit flat symbols; use authenticated account rows for
        # their configured leverage and margin mode, rather than inventing defaults.
        present = {(r["symbol"], r["positionSide"]) for r in rows}
        for row in account["positions"]:
            if row["symbol"] in symbols and (row["symbol"], row["positionSide"]) not in present:
                if type(row.get("isolated")) is not bool:
                    raise TradingError("账户全仓保证金模式响应无效")
                if dec(row["positionAmt"]):
                    raise TradingError("账户与持仓接口尚未同步")
                rows.append({"symbol": row["symbol"], "positionSide": row["positionSide"], "positionAmt": "0",
                             "entryPrice": "0", "markPrice": wire(self.market.book(row["symbol"]).mark),
                             "leverage": row["leverage"], "unRealizedProfit": "0", "liquidationPrice": "0",
                             "marginType": "isolated" if row["isolated"] else "cross"})
        positions = []
        for row in rows:
            if row["symbol"] not in symbols and not dec(row["positionAmt"]):
                continue
            if self.market.assets.get(row["symbol"]) != "USD1":
                if dec(row["positionAmt"]):
                    raise TradingError("检测到非 USD1 仓位，需要核对风险范围")
                continue
            positions.append(Position(row["symbol"], row["positionSide"], abs(dec(row["positionAmt"])), dec(row["entryPrice"]),
                positive(row["markPrice"]), int(row["leverage"]), dec(row["unRealizedProfit"]), positive(row["liquidationPrice"], True),
                isolated=row["marginType"].lower() not in ("cross", "crossed")))
        # Never combine balances and positions from different fills.
        account_positions = {(r["symbol"], r["positionSide"]): r for r in account["positions"]}
        for p in positions:
            other = account_positions.get((p.symbol, p.side))
            if not other or abs(dec(other["positionAmt"])) != p.qty or int(other["leverage"]) != p.leverage:
                raise TradingError("账户余额与持仓快照正在同步，稍后重试")
            if type(other.get("isolated")) is not bool or other["isolated"] != p.isolated:
                raise TradingError("账户与持仓的保证金模式尚未同步，稍后重试")
        brackets, fees = {}, {}
        for symbol in symbols:
            b = self.cached_call("bracket:" + symbol, "/fapi/v3/leverageBracket", {"symbol": symbol}, ttl=5)
            if isinstance(b, list):
                b = next((item for item in b if item.get("symbol") == symbol), {})
            if b.get("symbol") != symbol:
                raise TradingError("账户风控档位交易代码不匹配")
            brackets[symbol] = validate_brackets(b["brackets"])
            fee = self.cached_call("fee:" + symbol, "/fapi/v3/commissionRate", {"symbol": symbol}, ttl=60, weight=20)
            fees[symbol] = positive(fee["takerCommissionRate"], True)
        wallet = dec(asset["crossWalletBalance"])
        unrealized = dec(asset["crossUnPnl"])
        return AccountSnapshot(wallet + unrealized, positive(asset["maintMargin"], True), dec(asset["availableBalance"]), wallet,
            unrealized, positions, orders, dual.get("dualSidePosition") is True, multi.get("multiAssetsMargin") is True,
            account.get("canTrade") is True, started, fees, brackets)

    def set_leverage(self, symbol, leverage):
        snapshot = self.snapshot([symbol], fresh_modes=True)
        long, short = snapshot.require_ready(symbol)
        require_non_decreasing_leverage(long.leverage, leverage)
        if leverage == long.leverage:
            return {"symbol": symbol, "leverage": leverage}
        return self.api.call("POST", "/fapi/v3/leverage", {"symbol": symbol, "leverage": str(leverage)}, signed=True)

    def submit(self, orders):
        if len(orders) == 1:
            return [self.api.call("POST", "/fapi/v3/order", orders[0], signed=True)]
        return self.api.call("POST", "/fapi/v3/batchOrders", {"batchOrders": json.dumps(orders, separators=(",", ":"))}, signed=True, weight=5)

    def query(self, symbol, client_id):
        return self.api.call("GET", "/fapi/v3/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    def cancel(self, symbol, client_id):
        return self.api.call("DELETE", "/fapi/v3/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    def close(self):
        self.api.close()
