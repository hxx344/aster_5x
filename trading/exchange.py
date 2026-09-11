"""Aster V3 EIP-712 API adapter. No automatic retries of signed mutations."""
import json
from collections import deque
from contextlib import contextmanager, nullcontext
from email.utils import parsedate_to_datetime
from fractions import Fraction
import math
import os
import re
import threading
import time
from urllib.parse import urlencode

import httpx
from eth_account import Account as EthAccount
from eth_account.messages import encode_typed_data

import monitor
from .market_stream import PublicQuoteStream
from .models import AccountModeError, AccountSnapshot, Book, Position, Rules, SYMBOLS, TAKER_FEE_ESTIMATE, TradingError, dec, decimal_value, positive, require_non_decreasing_leverage, require_supported_leverage, validate_brackets, wire

BASE = "https://fapi.asterdex.com"


class ExchangeError(TradingError):
    def __init__(self, message, code=None, retry_after=0, *, http_status=None):
        super().__init__(message)
        self.code, self.retry_after = code, retry_after
        self.http_status = http_status


class AmbiguousOrder(ExchangeError):
    """The exchange might have accepted a mutation; query before deciding."""


class RequestNotSent(ExchangeError):
    """A local admission check failed before any HTTP request was sent."""


class LeverageRejected(ExchangeError):
    """The leverage mutation received a documented, definitive rejection."""


# Restrict this classification to documented rejection codes, not network or
# unknown processing failures. It is used only for the leverage POST below.
LEVERAGE_REJECTION_CODES = frozenset({
    -1002, -1003, -1011, -1015, -1020, -1022, -1100, -1101, -1102, -1103, -1104, -1105,
    -1106, -1111, -1121, -1130, -2014, -2015, -2019, -2027, -2028, -4028,
})
# Aster documents these as WAF/rate-limit/IP-ban rejections. Gateways may
# return HTML or no body, so the status itself must survive JSON parsing.
LEVERAGE_REJECTION_HTTP_STATUSES = frozenset({403, 418, 429})


class BudgetWait(RequestNotSent):
    """The local scheduler must wait; this is not an exchange rejection."""


class RateBudget:
    def __init__(self, reconciliation_reserve=300, capacity_reserve=0):
        if type(reconciliation_reserve) is not int or reconciliation_reserve < 0:
            raise ExchangeError("订单核对保留额度无效")
        if type(capacity_reserve) is not int or capacity_reserve < 0:
            raise ExchangeError("额度监控保留预算无效")
        self.lock = threading.Lock()
        self.until, self.window, self.weight = 0.0, time.monotonic(), 0
        self.deadline = self.window + 60
        self.server_minute = None
        self.local_weight, self.reported_weight = 0, None
        self.inflight, self.sequence = {}, 0
        self.unreported = deque()
        self.limit = 1800
        self.reconciliation_reserve = reconciliation_reserve
        self.capacity_reserve = capacity_reserve
        self.priority = threading.local()

    def configure_capacity_reserve(self, weight):
        if type(weight) is not int or weight < 0:
            raise ExchangeError("额度监控保留预算无效")
        with self.lock:
            self.capacity_reserve = weight

    @contextmanager
    def capacity_monitoring(self):
        """Keep shared quota discovery moving when account execution is busy."""
        previous = getattr(self.priority, "capacity_monitoring", False)
        self.priority.capacity_monitoring = True
        try:
            yield self
        finally:
            self.priority.capacity_monitoring = previous

    @contextmanager
    def reconciliation(self):
        """Let an existing intent finish without lending its quota to other threads."""
        previous = getattr(self.priority, "reconciliation", False)
        self.priority.reconciliation = True
        try:
            yield self
        finally:
            self.priority.reconciliation = previous

    def _refresh(self, now):
        if now >= self.deadline:
            steps = int((now - self.deadline) // 60) + 1
            if self.server_minute is not None:
                self.server_minute += steps
            self.deadline += steps * 60
            self.window = self.deadline - 60
            # Requests sent near the boundary may be counted in the new minute.
            self.weight = self.local_weight = self._carry(now)
            self.reported_weight = None

    def _carry(self, now):
        while self.unreported and self.unreported[0][0] <= now - 60:
            self.unreported.popleft()
        return sum(self.inflight.values()) + sum(weight for _, weight in self.unreported)

    def _ordinary_limit(self):
        return self.limit - min(self.reconciliation_reserve, self.limit // 6)

    def _execution_limit(self):
        ordinary_limit = self._ordinary_limit()
        return ordinary_limit - min(self.capacity_reserve, ordinary_limit // 3)

    def _require_available(self, weight, now):
        self._refresh(now)
        if now < self.until:
            raise RequestNotSent("接口退避中", retry_after=self.until - now)
        monitoring = getattr(self.priority, "capacity_monitoring", False)
        critical = getattr(self.priority, "reconciliation", False) and not monitoring
        if monitoring:
            limit = self._ordinary_limit()
        else:
            limit = self.limit if critical else self._execution_limit()
        if self.weight + weight > limit:
            message = "本地请求预算已用完" if critical else "本地普通请求预算不足，已为订单核对和补偿保留额度"
            if not critical and not monitoring and self.capacity_reserve:
                message = "本地执行请求预算不足，已为额度监控、订单核对和补偿保留额度"
            reported = "未返回" if self.reported_weight is None else str(self.reported_weight)
            message += f"（估算已用 {self.weight}/{limit}，本轮需 {weight}；本进程计入 {self.local_weight}，Aster 同 IP 回报 {reported}）"
            raise BudgetWait(message, retry_after=max(0.0, self.deadline - now))

    @staticmethod
    def _validate_weight(weight):
        if type(weight) is not int or weight <= 0:
            raise RequestNotSent("接口请求权重无效")

    def require_available(self, weight=200):
        """Check admission without spending; each actual request still reserves atomically."""
        self._validate_weight(weight)
        with self.lock:
            self._require_available(weight, time.monotonic())

    def reserve(self, weight, *, track=False):
        self._validate_weight(weight)
        with self.lock:
            now = time.monotonic()
            self._require_available(weight, now)
            self.weight += weight
            self.local_weight += weight
            if track:
                self.sequence += 1
                self.inflight[self.sequence] = weight
                return self.sequence
            self.unreported.append((now, weight))

    def finish(self, ticket):
        with self.lock:
            weight = self.inflight.pop(ticket, 0)
            if weight:
                # A timeout is not evidence that the exchange did not count it.
                self.unreported.append((time.monotonic(), weight))

    @staticmethod
    def _response_time(headers):
        try:
            date = parsedate_to_datetime(headers.get("Date", ""))
            stamp = date.timestamp()
            if date.tzinfo is not None and abs(stamp - time.time()) <= 300:
                return int(stamp)
        except (ValueError, TypeError, OverflowError, IndexError):
            pass
        return None

    def observe(self, headers, *, ticket=None):
        # Date identifies the exchange minute. Never treat a lower counter alone
        # as a reset: concurrent responses can arrive out of order.
        reported = headers.get("X-MBX-USED-WEIGHT-1M")
        valid_weight = isinstance(reported, str) and re.fullmatch(r"[0-9]{1,12}", reported)
        stamp = self._response_time(headers) if valid_weight else None
        with self.lock:
            now = time.monotonic()
            self._refresh(now)
            completed = self.inflight.pop(ticket, 0)
            if not valid_weight:
                if completed:
                    self.unreported.append((now, completed))
                return
            if stamp is not None:
                minute = stamp // 60
                if self.server_minute is not None and minute < self.server_minute:
                    return  # A delayed response must not revive last minute's use.
                if self.server_minute is not None and minute > self.server_minute:
                    self.weight = self.local_weight = completed + self._carry(now)
                    self.reported_weight = None
                if self.server_minute != minute:
                    self.server_minute = minute
                    # HTTP Date has one-second precision; allow that rounding and
                    # count from receipt, so the local reset cannot happen early.
                    self.deadline = now + 61 - stamp % 60
                    self.window = self.deadline - 60
            self.reported_weight = max(self.reported_weight or 0, int(reported))
            # A response may have been generated before an unreported request
            # reached Aster. Its counter cannot acknowledge that request merely
            # because our timeout (or headerless response) arrived first.
            self.weight = max(self.weight, self.reported_weight + self._carry(now))

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            self._refresh(now)
            ordinary_limit = self._ordinary_limit()
            execution_limit = self._execution_limit()
            reset_after = max(0.0, self.deadline - now)
            retry_after = max(0.0, self.until - now,
                              reset_after if self.weight >= execution_limit else 0.0)
            return {"used": self.weight, "limit": self.limit, "ordinary_limit": ordinary_limit,
                    "execution_limit": execution_limit, "capacity_reserve": ordinary_limit - execution_limit,
                    "local_used": self.local_weight, "aster_ip_used": self.reported_weight,
                    "remaining": max(0, self.limit - self.weight),
                    "ordinary_remaining": max(0, execution_limit - self.weight),
                    "reset_after": reset_after, "retry_after": retry_after}

    def block(self, seconds):
        try:
            seconds = float(seconds)
        except (TypeError, ValueError, OverflowError):
            seconds = 180.0
        if not math.isfinite(seconds) or seconds < 0:
            seconds = 180.0
        with self.lock:
            self.until = max(self.until, time.monotonic() + seconds)
        return seconds


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
        ticket = self.budget.reserve(weight, track=True)
        try:
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
            self.budget.observe(response.headers, ticket=ticket)
        finally:
            self.budget.finish(ticket)
        gateway_delay = 0
        if response.status_code in (403, 429, 418):
            delay = {403: 30, 429: 180, 418: 86400}[response.status_code]
            try:
                supplied_delay = float(response.headers.get("Retry-After", "0"))
                if math.isfinite(supplied_delay):
                    delay = max(delay, supplied_delay)
            except ValueError:
                pass
            self.budget.block(delay)
            if response.status_code != 403:
                raise ExchangeError("Aster 接口限流", retry_after=delay, http_status=response.status_code)
            # Preserve a WAF Retry-After even for non-JSON gateway pages, while
            # still honoring an explicit unknown-execution code in a JSON body.
            gateway_delay = delay
        if (response.status_code >= 500 or response.status_code == 408) and is_write:
            raise AmbiguousOrder("Aster 未确认请求结果，需核对订单", http_status=response.status_code)
        try:
            data = response.json()
        except ValueError:
            error = AmbiguousOrder if is_write else ExchangeError
            raise error("Aster 返回无法识别的响应", retry_after=gateway_delay, http_status=response.status_code) from None
        if isinstance(data, list) and any(isinstance(row, dict) and row.get("code") in (-1003, -1015) for row in data):
            # Batch responses may mix fills and rate-limit failures. Retain every
            # receipt so the executor can reconcile/repair the successful leg.
            self.budget.block(180)
        code = data.get("code") if isinstance(data, dict) else None
        if isinstance(code, int) and code < 0:
            if code in (-1003, -1015):
                self.budget.block(180)
                raise ExchangeError("Aster 请求或订单限流", code, retry_after=max(180, gateway_delay), http_status=response.status_code)
            if code in (-1006, -1007) and is_write:
                raise AmbiguousOrder("Aster 请求超时，需核对订单", code, retry_after=gateway_delay, http_status=response.status_code)
            raise ExchangeError(f"Aster 拒绝请求（代码 {code}）", code, retry_after=gateway_delay, http_status=response.status_code)
        if not response.is_success:
            raise ExchangeError(f"Aster HTTP {response.status_code}", code, retry_after=gateway_delay, http_status=response.status_code)
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
    def __init__(self, api=None, *, stream=None):
        self.api = api or API()
        self.stream = stream if stream is not None else PublicQuoteStream()
        self.rules = {}
        self.assets = {}
        self.books, self.book_locks = {}, {}
        self.book_guard = threading.Lock()

    def start_stream(self):
        self.stream.start()

    def close_stream(self):
        self.stream.close()

    @staticmethod
    def market_quantity_limits(lot, market_lot=None):
        """Use quantities satisfying both general and market-specific lot filters."""
        minimums, maximums, steps = [], [], []
        for constraint in (lot,) if market_lot is None else (lot, market_lot):
            if not isinstance(constraint, dict) or any(key not in constraint for key in ("minQty", "maxQty", "stepSize")):
                raise TradingError("市价数量过滤器响应无效")
            minimum, maximum, step = (positive(constraint[key], True) for key in ("minQty", "maxQty", "stepSize"))
            minimums.append(minimum)
            if maximum:
                maximums.append(maximum)
            if step:
                # Rules/plan_pair use a zero-origin grid. Do not silently turn an
                # exchange-supplied offset grid into quantities it would reject.
                if Fraction(minimum) % Fraction(step):
                    raise TradingError("市价数量下限与步长不对齐，无法安全生成委托数量")
                steps.append(Fraction(step))
        if not steps or not maximums:
            # quantityPrecision is expressly not a substitute for stepSize.
            raise TradingError("市价数量规则缺少有效步长或数量上限")
        common_step = Fraction(math.lcm(*(s.numerator for s in steps)), math.gcd(*(s.denominator for s in steps)))
        step = decimal_value(common_step, exact=True)
        minimum, maximum = max(*minimums, step), min(maximums)
        first_lot = -(-Fraction(minimum) // common_step)
        if first_lot * common_step > Fraction(maximum):
            raise TradingError("市价数量上下限没有有效步进，暂停该规则加载")
        return step, minimum, maximum

    def load_rules(self):
        data = self.api.call("GET", "/fapi/v3/exchangeInfo")
        if not isinstance(data, dict) or not isinstance(data.get("symbols"), list):
            raise TradingError("交易规则响应无效")
        rules, assets = {}, {}
        for row in data["symbols"]:
            assets[row["symbol"]] = row["marginAsset"]
            # All assets are needed to account for outside holdings, but only the
            # configured strategy universe needs executable MARKET quantity rules.
            if row.get("status") != "TRADING" or row["symbol"] not in SYMBOLS:
                continue
            order_types = row.get("orderTypes", row.get("OrderType"))
            if order_types is not None:
                if not isinstance(order_types, list) or any(not isinstance(kind, str) for kind in order_types):
                    raise TradingError("交易订单类型响应无效")
                if "MARKET" not in order_types:
                    continue
            if not isinstance(row.get("filters"), list):
                raise TradingError("交易过滤器响应无效")
            filters = {}
            for constraint in row["filters"]:
                if not isinstance(constraint, dict) or not isinstance(constraint.get("filterType"), str):
                    raise TradingError("交易过滤器响应无效")
                kind = constraint["filterType"]
                if kind in filters:
                    raise TradingError("交易过滤器重复，无法确认市价数量规则")
                filters[kind] = constraint
            lot, price = filters.get("LOT_SIZE"), filters.get("PRICE_FILTER")
            notional = filters.get("MIN_NOTIONAL", filters.get("NOTIONAL", {}))
            if lot and price and ("notional" in notional or "minNotional" in notional):
                step, minimum, maximum = self.market_quantity_limits(lot, filters.get("MARKET_LOT_SIZE"))
                rules[row["symbol"]] = Rules(row["symbol"], step, positive(price["tickSize"]), minimum, maximum,
                    positive(notional.get("notional", notional.get("minNotional")), True), row["marginAsset"])
        self.rules, self.assets = rules, assets
        for rate in data.get("rateLimits", []):
            if rate.get("rateLimitType") == "REQUEST_WEIGHT" and rate.get("interval") == "MINUTE" and rate.get("intervalNum") == 1:
                self.api.budget.limit = min(1800, max(1, int(rate["limit"] * .8)))

    def _stream_book(self, symbol):
        try:
            streamed = self.stream.book(symbol)
            if streamed is not None:
                streamed.require_fresh()
                # Only the stream owns WS quotes. A disconnected stream must
                # not leave a second usable copy in the REST fallback cache.
                return streamed
        except TradingError:
            pass
        return None

    def book(self, symbol):
        # A recovered WS feed must not wait behind an in-flight REST fallback.
        streamed = self._stream_book(symbol)
        if streamed is not None:
            return streamed
        with self.book_guard:
            lock = self.book_locks.setdefault(symbol, threading.Lock())
        with lock:
            streamed = self._stream_book(symbol)
            if streamed is not None:
                return streamed
            cached = self.books.get(symbol)
            if cached is not None and time.monotonic() - cached[0] < 1:
                try:
                    cached[1].require_fresh()
                    return cached[1]
                except TradingError:
                    pass
            self.books.pop(symbol, None)
            started = time.monotonic()
            book = self._read_book(symbol)
            # Network time is part of cache age; a slow read cannot renew it.
            self.books[symbol] = (started, book)
            return book

    def _read_book(self, symbol):
        # Fetch mark first so the executable BBO is as recent as possible.
        mark = self.api.call("GET", "/fapi/v3/premiumIndex", {"symbol": symbol})
        row = self.api.call("GET", "/fapi/v3/ticker/bookTicker", {"symbol": symbol}, weight=1)
        if not isinstance(row, dict) or not isinstance(mark, dict):
            raise TradingError("报价响应无效")
        if row.get("symbol") != symbol or mark.get("symbol") != symbol:
            raise TradingError("报价交易代码不匹配")
        timestamps = [float(positive(source.get("time"))) / 1000 for source in (row, mark)]
        now = time.time()
        if any(not -1 <= now - stamp <= 3 for stamp in timestamps):
            raise TradingError("BBO 或标记价格时间无效，等待新报价")
        book = Book(positive(row["bidPrice"]), positive(row["askPrice"]), positive(row["bidQty"]),
                    positive(row["askQty"]), positive(mark["markPrice"]), min(timestamps))
        book.require_fresh(now)
        return book

    def capacities(self, symbol, leverages):
        with self.api.budget.capacity_monitoring():
            self.api.budget.reserve(2)
        try:
            result = monitor.sample({"symbol": symbol, "leverages": sorted(set(leverages)), "timeout_seconds": 8})
            return {v: positive(row["value"], True) for v, row in result.items() if not isinstance(row, Exception)}
        except monitor.MonitorError as exc:
            delay = self.api.budget.block(exc.retry_after) if exc.retry_after else 0
            raise ExchangeError(str(exc), retry_after=delay) from None


class LiveBroker:
    mode = "live"

    def __init__(self, credentials, market, api=None):
        self.api = api or API(credentials)
        self.market = market
        self.cached, self.cached_at = {}, {}
        self.leverage_snapshot = None

    def reconciliation_budget(self):
        budget = getattr(self.api, "budget", None)
        return budget.reconciliation() if budget is not None else nullcontext()

    def cached_call(self, key, path, params=None, ttl=300, weight=1):
        started = time.monotonic()
        if started - self.cached_at.get(key, -1e9) >= ttl:
            self.cached[key] = self.api.call("GET", path, params, signed=True, weight=weight)
            # Network time counts towards cache age; slow reads must not renew it.
            self.cached_at[key] = started
        return self.cached[key]

    def snapshot_weight(self, symbols, *, fresh_modes=False):
        """Conservative admission estimate using cache expiries, without a request."""
        now = time.monotonic()
        def due(key, ttl):
            # Leave time for this read to complete before reusing an expiring item.
            return now - self.cached_at.get(key, -1e9) + 8 >= ttl
        weight = 10 + 2 * len(symbols)  # Balances, all positions, flat marks.
        weight += sum(30 for key in ("dual", "multi") if fresh_modes or due(key, 15))
        weight += sum(1 for symbol in symbols if due("bracket:" + symbol, 5))
        return weight

    def snapshot(self, symbols, fresh_modes=False):
        self.leverage_snapshot = None
        started = time.time()
        if fresh_modes:
            self.cached_at.pop("dual", None)
            self.cached_at.pop("multi", None)
        dual = self.cached_call("dual", "/fapi/v3/positionSide/dual", ttl=15, weight=30)
        multi = self.cached_call("multi", "/fapi/v3/multiAssetsMargin", ttl=15, weight=30)
        if (not isinstance(dual, dict) or not isinstance(multi, dict)
                or type(dual.get("dualSidePosition")) is not bool or type(multi.get("multiAssetsMargin")) is not bool):
            raise TradingError("账户持仓或保证金模式响应无效")
        account = self.api.call("GET", "/fapi/v3/accountWithJoinMargin", signed=True, weight=5)
        rows = self.api.call("GET", "/fapi/v3/positionRisk", signed=True, weight=5)
        if (not isinstance(account, dict) or not isinstance(account.get("assets"), list)
                or any(not isinstance(a, dict) for a in account["assets"])):
            raise TradingError("账户资产响应无效")
        assets = [a for a in account["assets"] if a.get("asset") == "USD1"]
        if not assets:
            raise TradingError("账户缺少 USD1 保证金资产")
        if len(assets) != 1:
            raise TradingError("账户 USD1 保证金资产重复")
        asset = assets[0]
        account_positions = self._position_rows(account.get("positions"))
        present = self._position_rows(rows)
        # Some V3 responses omit flat symbols; use authenticated account rows for
        # their configured leverage and margin mode, rather than inventing defaults.
        rows, flat_marks = list(rows), {}
        for row in account_positions.values():
            if row["symbol"] in symbols and (row["symbol"], row["positionSide"]) not in present:
                if type(row.get("isolated")) is not bool:
                    raise TradingError("账户全仓保证金模式响应无效")
                if dec(row["positionAmt"]):
                    raise TradingError("账户与持仓接口尚未同步")
                if row["symbol"] not in flat_marks:
                    flat_marks[row["symbol"]] = wire(self.market.book(row["symbol"]).mark)
                rows.append({"symbol": row["symbol"], "positionSide": row["positionSide"], "positionAmt": "0",
                             "entryPrice": "0", "markPrice": flat_marks[row["symbol"]],
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
            qty = dec(row["positionAmt"]).copy_abs()
            entry = positive(row["entryPrice"], allow_zero=not qty)
            positions.append(Position(row["symbol"], row["positionSide"], qty, entry,
                positive(row["markPrice"]), self._leverage(row.get("leverage")), dec(row["unRealizedProfit"]), positive(row["liquidationPrice"], True),
                isolated=row["marginType"].lower() not in ("cross", "crossed")))
        # Never combine balances and positions from different fills.
        represented = {(p.symbol, p.side) for p in positions}
        if any(dec(row["positionAmt"]) and key not in represented for key, row in account_positions.items()):
            raise TradingError("账户全部持仓尚未同步，无法计算总占用保证金")
        for p in positions:
            other = account_positions.get((p.symbol, p.side))
            if not other or dec(other["positionAmt"]).copy_abs() != p.qty or self._leverage(other.get("leverage")) != p.leverage:
                raise TradingError("账户余额与持仓快照正在同步，稍后重试")
            if type(other.get("isolated")) is not bool or other["isolated"] != p.isolated:
                raise TradingError("账户与持仓的保证金模式尚未同步，稍后重试")
        brackets = {}
        for symbol in symbols:
            b = self.cached_call("bracket:" + symbol, "/fapi/v3/leverageBracket", {"symbol": symbol}, ttl=5)
            if isinstance(b, list):
                if any(not isinstance(item, dict) for item in b):
                    raise TradingError("账户风控档位响应无效")
                matches = [item for item in b if item.get("symbol") == symbol]
                if len(matches) != 1:
                    raise TradingError("账户风控档位缺失或重复")
                b = matches[0]
            if not isinstance(b, dict) or b.get("symbol") != symbol:
                raise TradingError("账户风控档位交易代码不匹配")
            brackets[symbol] = validate_brackets(b["brackets"])
        wallet = dec(asset["crossWalletBalance"])
        account_unrealized = Fraction(dec(asset["crossUnPnl"]))
        # Account balances and position marks are separate reads. Charge newer
        # losses immediately, but never fund additions with an unconfirmed gain.
        position_unrealized = sum((Fraction(p.unrealized) for p in positions), Fraction(0))
        marked_unrealized = sum((Fraction(p.qty) * (Fraction(p.mark) - Fraction(p.entry)) * (1 if p.side == "LONG" else -1)
                                 for p in positions), Fraction(0))
        selected_pnl = min(account_unrealized, position_unrealized, marked_unrealized)
        unrealized = decimal_value(selected_pnl, exact=True)
        available = decimal_value(Fraction(dec(asset["availableBalance"])) - (account_unrealized - selected_pnl), exact=True)
        equity = decimal_value(Fraction(wallet) + selected_pnl, exact=True)
        # External orders are not queried; None must not imply a verified empty
        # order book. Fees are a fixed planning estimate, not a fetched fee rate.
        snapshot = AccountSnapshot(equity, positive(asset["maintMargin"], True), available, wallet,
            unrealized, positions, None, dual.get("dualSidePosition") is True, multi.get("multiAssetsMargin") is True,
            account.get("canTrade") is True, started, dict.fromkeys(symbols, TAKER_FEE_ESTIMATE), brackets)
        if fresh_modes:
            self.leverage_snapshot = (snapshot, time.monotonic())
        return snapshot

    @staticmethod
    def _position_rows(rows):
        if not isinstance(rows, list):
            raise TradingError("持仓响应无效")
        result = {}
        for row in rows:
            if (not isinstance(row, dict) or not isinstance(row.get("symbol"), str) or not row["symbol"]
                    or row.get("positionSide") not in ("LONG", "SHORT", "BOTH")):
                raise TradingError("持仓交易代码或方向无效")
            quantity = dec(row.get("positionAmt"))
            if quantity and row["positionSide"] == "BOTH":
                raise AccountModeError("双向账户出现单向持仓，需核对账户模式")
            key = (row["symbol"], row["positionSide"])
            if key in result:
                raise TradingError("持仓接口返回重复记录，无法核对总占用保证金")
            result[key] = row
        return result

    @staticmethod
    def _leverage(value):
        leverage = positive(value)
        if not 1 <= leverage <= 125 or leverage != leverage.to_integral_value():
            raise TradingError("账户实际杠杆无效")
        return int(leverage)

    def set_leverage(self, symbol, leverage, *, checked_snapshot=None, before_submit=None):
        require_supported_leverage(leverage)
        verified, self.leverage_snapshot = self.leverage_snapshot, None
        snapshot = (checked_snapshot if verified is not None and checked_snapshot is verified[0]
                    and 0 <= time.monotonic() - verified[1] <= 1 else None)
        if snapshot is not None:
            try:
                snapshot.require_fresh()
            except TradingError:
                snapshot = None
        if snapshot is None:
            snapshot = self.snapshot([symbol], fresh_modes=True)
            self.leverage_snapshot = None
        snapshot.require_modes([symbol])
        long, short = snapshot.require_ready(symbol)
        require_non_decreasing_leverage(long.leverage, leverage)
        if leverage == long.leverage:
            return {"symbol": symbol, "leverage": leverage}
        if before_submit is not None:
            try:
                # This is the last account read, including the stale-token fallback.
                before_submit(snapshot)
                snapshot.require_ready(symbol)
            except RequestNotSent:
                raise
            except TradingError as exc:
                raise RequestNotSent(str(exc), retry_after=getattr(exc, "retry_after", 0)) from None
        try:
            return self.api.call("POST", "/fapi/v3/leverage", {"symbol": symbol, "leverage": str(leverage)}, signed=True)
        except ExchangeError as exc:
            status_rejected = exc.http_status in LEVERAGE_REJECTION_HTTP_STATUSES and exc.code not in (-1006, -1007)
            code_rejected = not isinstance(exc, AmbiguousOrder) and exc.code in LEVERAGE_REJECTION_CODES
            if not isinstance(exc, RequestNotSent) and (status_rejected or code_rejected):
                raise LeverageRejected(str(exc), code=exc.code, retry_after=max(30, exc.retry_after),
                                       http_status=exc.http_status) from None
            raise

    def submit(self, orders):
        self.leverage_snapshot = None
        if len(orders) == 1:
            return [self.api.call("POST", "/fapi/v3/order", orders[0], signed=True)]
        return self.api.call("POST", "/fapi/v3/batchOrders", {"batchOrders": json.dumps(orders, separators=(",", ":"))}, signed=True, weight=5)

    def query(self, symbol, client_id):
        with self.reconciliation_budget():
            return self.api.call("GET", "/fapi/v3/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    def cancel(self, symbol, client_id):
        with self.reconciliation_budget():
            return self.api.call("DELETE", "/fapi/v3/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    def close(self):
        self.api.close()
