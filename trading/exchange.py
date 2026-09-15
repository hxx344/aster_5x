"""Aster V3 EIP-712 API adapter. No automatic retries of signed mutations."""
import json
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, nullcontext
from dataclasses import replace
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
from .account_cache import CycleAccountCache, HotAccountUnavailable
from .depth import DEPTH_LIMIT, DEPTH_MAX_AGE, DEPTH_RESYNC_INTERVAL, DEPTH_WEIGHT, DepthSnapshot
from .depth_stream import PublicDepthStream
from .market_stream import PublicQuoteStream
from .user_stream import PrivateAccountStream
from .models import AccountModeError, AccountSnapshot, Book, Position, Rules, SYMBOLS, TAKER_FEE_ESTIMATE, TradingError, dec, decimal_value, leverage_cap, positive, require_non_decreasing_leverage, require_supported_leverage, validate_brackets, wire

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

    @contextmanager
    def cycle_accounting(self):
        """Fill reporting cannot consume the reserved order-repair budget."""
        previous = getattr(self.priority, "cycle_accounting", False)
        self.priority.cycle_accounting = True
        try:
            yield self
        finally:
            self.priority.cycle_accounting = previous

    def _priority_flags(self):
        return tuple(getattr(self.priority, name, False) for name in
                     ("reconciliation", "cycle_accounting", "capacity_monitoring"))

    @contextmanager
    def _inherit_priority(self, flags):
        previous = self._priority_flags()
        names = ("reconciliation", "cycle_accounting", "capacity_monitoring")
        for name, value in zip(names, flags):
            setattr(self.priority, name, value)
        try:
            yield
        finally:
            for name, value in zip(names, previous):
                setattr(self.priority, name, value)

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
        critical = (getattr(self.priority, "reconciliation", False) and not monitoring
                    and not getattr(self.priority, "cycle_accounting", False))
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
    def __init__(self, api=None, *, stream=None, depth_stream=None):
        self.api = api or API()
        self.stream = stream if stream is not None else PublicQuoteStream()
        self.depth_stream = depth_stream if depth_stream is not None else PublicDepthStream()
        self.rules = {}
        self.assets = {}
        self.books, self.book_locks = {}, {}
        self.book_guard = threading.Lock()
        self.depth_locks = {symbol: threading.Lock() for symbol in SYMBOLS}
        self.depth_retry_at = {}

    def set_update_listener(self, listener):
        """Forward optional market signals while supporting older injected streams."""
        if listener is not None and not callable(listener):
            raise ValueError("Invalid market update listener")
        for stream in (self.stream, self.depth_stream):
            setter = getattr(stream, "set_update_listener", None)
            if callable(setter):
                setter(listener)

    def start_stream(self):
        try:
            self.stream.start()
        finally:
            self.depth_stream.start()

    def close_stream(self):
        try:
            self.stream.close()
        finally:
            self.depth_stream.close()

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

    def cycle_book(self, symbol):
        """A trading trigger reads only the current local WS book."""
        book = self._stream_book(symbol)
        if book is None:
            raise HotAccountUnavailable("循环报价热数据尚未就绪，等待行情更新")
        book.require_fresh()
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

    def depth_weight(self, symbols):
        """Only missing stream seeds may require REST; healthy reads are free."""
        return sum(DEPTH_WEIGHT for symbol in set(symbols)
                   if self.depth_stream.seed_token(symbol) is not None
                   and time.monotonic() >= self.depth_retry_at.get(symbol, 0))

    def depth(self, symbol):
        """Read shared WS depth, obtaining REST only to seed a connected stream."""
        if symbol not in SYMBOLS:
            raise TradingError("不支持的深度市场")
        depth = self.depth_stream.snapshot(symbol)
        if depth is not None:
            depth.require_fresh()
            return depth

        with self.depth_locks[symbol]:
            depth = self.depth_stream.snapshot(symbol)
            if depth is not None:
                depth.require_fresh()
                return depth
            token = self.depth_stream.seed_token(symbol)
            if token is None:
                raise TradingError("深度 WS 正在连接或同步，等待有效推送")
            retry_after = self.depth_retry_at.get(symbol, 0) - time.monotonic()
            if retry_after > 0:
                raise ExchangeError("深度正在重新同步，等待重试", retry_after=retry_after)
            # Per-symbol single-flight and cooldown also bound repeated gaps or
            # reconnects. Never turn a failed WS feed into continuous REST polls.
            self.depth_retry_at[symbol] = time.monotonic() + DEPTH_RESYNC_INTERVAL
            started, requested_at = time.monotonic(), time.time()
            try:
                data = self._read_depth(symbol)
            except TradingError as exc:
                self.depth_retry_at[symbol] = max(self.depth_retry_at[symbol],
                    time.monotonic() + getattr(exc, "retry_after", 0))
                raise
            if time.monotonic() - started > DEPTH_MAX_AGE:
                raise TradingError("深度请求耗时过长，等待更新")
            self.depth_stream.seed(symbol, data, token=token, requested_at=requested_at)
            depth = self.depth_stream.snapshot(symbol)
            if depth is None:
                raise TradingError("深度 WS 正在同步，等待连续更新")
            depth.require_fresh()
            return depth

    def cycle_depth(self, symbol):
        """Read local bridged depth without starting a REST seed on demand."""
        if symbol not in SYMBOLS:
            raise TradingError("不支持的深度市场")
        depth = self.depth_stream.snapshot(symbol)
        if depth is None:
            raise HotAccountUnavailable("循环深度热数据尚未就绪，等待行情更新")
        depth.require_fresh()
        return depth

    def _read_depth(self, symbol):
        """Raw REST seed; never expose an unbridged snapshot to consumers."""
        started, requested_at = time.monotonic(), time.time()
        data = self.api.call("GET", "/fapi/v3/depth", {"symbol": symbol, "limit": DEPTH_LIMIT}, weight=DEPTH_WEIGHT)
        if time.monotonic() - started > DEPTH_MAX_AGE:
            raise TradingError("深度请求耗时过长，等待更新")
        if isinstance(data, dict) and data.get("symbol", symbol) != symbol:
            raise TradingError("深度交易代码不匹配")
        DepthSnapshot.from_response(data, requested_at=requested_at)
        return data

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
        self._snapshot_lock = threading.RLock()
        self._cycle_lifecycle_lock = threading.Lock()
        self._cycle_stream_callback_lock = threading.RLock()
        self._cycle_stream_token = None
        self._cycle_hot_closed = False
        self.cycle_cache = CycleAccountCache()
        self.cycle_stream = None
        self.leverage_snapshot = None
        self.cycle_trade_queries = {}

    def start_cycle_hot_data(self, symbols, *, on_invalidate=None):
        """Configure one private stream; neither construction nor start reads REST here."""
        with self._cycle_lifecycle_lock:
            if self._cycle_hot_closed:
                raise HotAccountUnavailable("循环账户热数据已关闭")
            self.cycle_cache.set_listener(on_invalidate)
            self.cycle_cache.configure(symbols)
            if self.cycle_stream is None:
                token = object()
                stream = PrivateAccountStream(self.api,
                    on_state=lambda connected: self._cycle_stream_state(token, connected),
                    on_event=lambda kind: self._cycle_stream_event(token, kind))
                with self._cycle_stream_callback_lock:
                    self._cycle_stream_token = token
                self.cycle_stream = stream
            self.cycle_stream.start()

    def stop_cycle_hot_data(self):
        """Stop private monitoring while keeping this broker's API reusable."""
        with self._cycle_lifecycle_lock:
            stream, self.cycle_stream = self.cycle_stream, None
            with self._cycle_stream_callback_lock:
                self._cycle_stream_token = None
                self.cycle_cache.set_listener(None)
                self.cycle_cache.set_connected(False)
            if stream is not None:
                stream.close()

    def _cycle_stream_state(self, token, connected):
        # A stopped stream may finish after a replacement has connected.
        with self._cycle_stream_callback_lock:
            if token is self._cycle_stream_token:
                self.cycle_cache.set_connected(connected)

    def _cycle_stream_event(self, token, kind):
        with self._cycle_stream_callback_lock:
            if token is self._cycle_stream_token:
                self._cycle_account_event(kind)

    def invalidate_cycle_hot_data(self, reason, *, refresh_modes=False):
        self.cycle_cache.invalidate(reason, refresh_modes=refresh_modes)

    def _cycle_account_event(self, kind):
        self.cycle_cache.invalidate("账户事件：" + str(kind),
            refresh_modes=kind not in ("ACCOUNT_UPDATE", "ORDER_TRADE_UPDATE"))

    def _cycle_write(self, method, path, params, *, weight=1, refresh_modes=False):
        # No snapshot HTTP lock: in-flight background results are revoked by
        # generation instead of making a ready order wait for another request.
        self.leverage_snapshot = None
        self.cycle_cache.invalidate("账户写入开始", refresh_modes=refresh_modes)
        try:
            return self.api.call(method, path, params, signed=True, weight=weight)
        finally:
            # A refresh that started during this write must also be discarded.
            self.leverage_snapshot = None
            self.cycle_cache.invalidate("账户写入结束", refresh_modes=refresh_modes)

    def refresh_cycle_hot_snapshot(self):
        """Only the background scheduler calls this network refresh."""
        ticket = self.cycle_cache.begin_refresh()
        started = time.monotonic()
        try:
            # Ordinary and cycle parsers share small mode/tier caches. Their
            # network reads serialize here, but leases and writes never take it.
            with self._snapshot_lock:
                try:
                    snapshot = self.cycle_snapshot(ticket.symbols, fresh_modes=ticket.refresh_modes)
                    valid_until = self.cached_at["multi"] + 15
                finally:
                    # Background maintenance never grants a leverage-write token.
                    self.leverage_snapshot = None
            snapshot.require_modes(ticket.symbols)
            return self.cycle_cache.publish(ticket, snapshot, started,
                valid_until_monotonic=valid_until)
        except BaseException as exc:
            self.cycle_cache.fail(ticket, exc)
            raise

    def cycle_hot_snapshot(self, symbols):
        """Lease account state and revalue locally, without any HTTP or read lock."""
        lease = self.cycle_cache.lease(symbols)
        snapshot = lease.snapshot
        marks = {symbol: self._cycle_local_mark(symbol) for symbol in dict.fromkeys(p.symbol for p in snapshot.positions)}
        positions = [replace(position, mark=marks[position.symbol] if marks[position.symbol] is not None else position.mark)
                     for position in snapshot.positions]
        marked = sum((Fraction(position.qty) * (Fraction(position.mark) - Fraction(position.entry))
                      * (1 if position.side == "LONG" else -1) for position in positions), Fraction(0))
        pnl = min(Fraction(snapshot.unrealized), marked)
        loss = Fraction(snapshot.unrealized) - pnl
        lease.snapshot = replace(snapshot, positions=positions, unrealized=decimal_value(pnl, exact=True),
            equity=decimal_value(Fraction(snapshot.equity) - loss, exact=True),
            available=decimal_value(Fraction(snapshot.available) - loss, exact=True))
        lease.require_fresh()
        return lease

    def reconciliation_budget(self):
        budget = getattr(self.api, "budget", None)
        return budget.reconciliation() if budget is not None else nullcontext()

    def cycle_volume_budget(self):
        budget = getattr(self.api, "budget", None)
        return budget.cycle_accounting() if budget is not None else nullcontext()

    def cached_call(self, key, path, params=None, ttl=300, weight=1):
        with self._snapshot_lock:
            started = time.monotonic()
            if started - self.cached_at.get(key, -1e9) >= ttl:
                self.cached[key] = self.api.call("GET", path, params, signed=True, weight=weight)
                # Network time counts towards cache age; slow reads must not renew it.
                self.cached_at[key] = started
            return self.cached[key]

    def snapshot_weight(self, symbols, *, fresh_modes=False):
        with self._snapshot_lock:
            return self._snapshot_weight(symbols, fresh_modes=fresh_modes)

    def _snapshot_weight(self, symbols, *, fresh_modes=False):
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
        with self._snapshot_lock:
            return self._snapshot(symbols, fresh_modes=fresh_modes)

    def cycle_snapshot_weight(self, symbols, *, fresh_modes=False):
        with self._snapshot_lock:
            return self._cycle_snapshot_weight(symbols, fresh_modes=fresh_modes)

    def _cycle_snapshot_weight(self, symbols, *, fresh_modes=False):
        """Include conditional risk/flat-mark/tier reads without issuing them."""
        symbols = tuple(dict.fromkeys(symbols))
        now = time.monotonic()
        # Account; reserve for one full risk fallback and the
        # existing two-request flat quote fallback when risk omits a symbol.
        weight = 5 + 5 + 2 * len(symbols)
        if fresh_modes or now - self.cached_at.get("multi", -1e9) + 8 >= 15:
            weight += 30
        weight += sum(1 for symbol in symbols if now - self.cached_at.get("bracket:" + symbol, -1e9) + 8 >= 5)
        return weight

    @staticmethod
    def _account_asset(account):
        if (not isinstance(account, dict) or not isinstance(account.get("assets"), list)
                or any(not isinstance(a, dict) for a in account["assets"])):
            raise TradingError("账户资产响应无效")
        assets = [a for a in account["assets"] if a.get("asset") == "USD1"]
        if not assets:
            raise TradingError("账户缺少 USD1 保证金资产")
        if len(assets) != 1:
            raise TradingError("账户 USD1 保证金资产重复")
        return assets[0]

    @staticmethod
    def _account_snapshot(account, asset, positions, symbols, *, hedge, multi, started, brackets=None,
                          current_caps=None, risk_unrealized=None):
        wallet = dec(asset["crossWalletBalance"])
        account_unrealized = Fraction(dec(asset["crossUnPnl"]))
        # Charge newer losses immediately, without funding additions with an
        # unconfirmed gain. All USD1 positions contribute, including other markets.
        position_unrealized = sum((Fraction(p.unrealized) for p in positions), Fraction(0))
        marked_unrealized = sum((Fraction(p.qty) * (Fraction(p.mark) - Fraction(p.entry)) * (1 if p.side == "LONG" else -1)
                                 for p in positions), Fraction(0))
        selected_pnl = min(account_unrealized, position_unrealized, marked_unrealized)
        if risk_unrealized is not None:
            selected_pnl = min(selected_pnl, risk_unrealized)
        unrealized = decimal_value(selected_pnl, exact=True)
        available = decimal_value(Fraction(dec(asset["availableBalance"])) - (account_unrealized - selected_pnl), exact=True)
        equity = decimal_value(Fraction(wallet) + selected_pnl, exact=True)
        return AccountSnapshot(equity, positive(asset["maintMargin"], True), available, wallet,
            unrealized, positions, None, hedge, multi, account.get("canTrade") is True,
            started, dict.fromkeys(symbols, TAKER_FEE_ESTIMATE), brackets or {}, current_caps or {})

    def _snapshot(self, symbols, fresh_modes=False, *, read=None, started=None):
        """Shared parsing; the ordinary path retains its sequential reads."""
        self.leverage_snapshot = None
        started = time.time() if started is None else started
        if read is None:
            def read(key, path, params=None, *, ttl=None, weight=1):
                if ttl is not None:
                    return self.cached_call(key, path, params, ttl=ttl, weight=weight)
                return self.api.call("GET", path, params, signed=True, weight=weight)
        if fresh_modes:
            self.cached_at.pop("dual", None)
            self.cached_at.pop("multi", None)
        dual = read("dual", "/fapi/v3/positionSide/dual", ttl=15, weight=30)
        multi = read("multi", "/fapi/v3/multiAssetsMargin", ttl=15, weight=30)
        if (not isinstance(dual, dict) or not isinstance(multi, dict)
                or type(dual.get("dualSidePosition")) is not bool or type(multi.get("multiAssetsMargin")) is not bool):
            raise TradingError("账户持仓或保证金模式响应无效")
        account = read("account", "/fapi/v3/accountWithJoinMargin", weight=5)
        rows = read("positions", "/fapi/v3/positionRisk", weight=5)
        asset = self._account_asset(account)
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
            b = read("bracket:" + symbol, "/fapi/v3/leverageBracket", {"symbol": symbol}, ttl=5)
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
        # External orders are not queried; None must not imply a verified empty
        # order book. Fees are a fixed planning estimate, not a fetched fee rate.
        snapshot = self._account_snapshot(account, asset, positions, symbols, hedge=dual["dualSidePosition"],
            multi=multi["multiAssetsMargin"], started=started, brackets=brackets)
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

    def _cycle_local_mark(self, symbol):
        """Use only local stream state here; live market.book may issue REST."""
        try:
            if getattr(self.market, "demo", False) is True:
                book = self.market.book(symbol)
            elif callable(getattr(self.market, "_stream_book", None)):
                book = self.market._stream_book(symbol)
            else:
                stream = getattr(self.market, "stream", None)
                book = stream.book(symbol) if callable(getattr(stream, "book", None)) else None
            if book is not None:
                book.require_fresh()
                return positive(book.mark)
        except (TradingError, KeyError):
            pass
        return None

    def _cycle_positions(self, account, symbols, read):
        rows = self._position_rows(account.get("positions"))
        # V3 account returns LONG/SHORT only in hedge mode, including flat legs.
        # Do not infer a missing leg as flat or manufacture a dual-mode GET cache.
        if any(side == "BOTH" for _, side in rows):
            raise AccountModeError("独立循环需要双向持仓模式，账户返回单向持仓信息")
        for symbol in symbols:
            if any((symbol, side) not in rows for side in ("LONG", "SHORT")):
                raise TradingError(f"{symbol} 缺少双向持仓信息")
        relevant = {key: row for key, row in rows.items() if key[0] in symbols or dec(row["positionAmt"])}
        leverages = {}
        for (symbol, side), row in relevant.items():
            if self.market.assets.get(symbol) != "USD1":
                raise TradingError("检测到非 USD1 仓位，需要核对风险范围")
            if type(row.get("isolated")) is not bool:
                raise TradingError("账户全仓保证金模式响应无效")
            leverage = self._leverage(row.get("leverage"))
            if symbol in leverages and leverages[symbol] != leverage:
                raise TradingError("多空杠杆不一致")
            leverages[symbol] = leverage
            positive(row.get("entryPrice"), allow_zero=not dec(row["positionAmt"]))
            dec(row.get("unrealizedProfit"))

        marks = {symbol: self._cycle_local_mark(symbol) for symbol in dict.fromkeys(key[0] for key in relevant)}
        risk, risk_unrealized = {}, None
        if any(mark is None for mark in marks.values()):
            risk = self._position_rows(read("positions", "/fapi/v3/positionRisk", weight=5))
            if any(side == "BOTH" for _, side in risk):
                raise AccountModeError("账户与持仓的双向模式尚未同步")
            # Once a full risk response is needed, every nonzero row on either
            # side must agree; a newly filled external position cannot disappear.
            for key in set(relevant) | {key for key, row in risk.items() if dec(row["positionAmt"])}:
                account_row, risk_row = rows.get(key), risk.get(key)
                if risk_row is None and account_row is not None and not dec(account_row["positionAmt"]):
                    continue  # Risk is allowed to omit flat account rows.
                if (account_row is None or risk_row is None
                        or dec(account_row["positionAmt"]) != dec(risk_row["positionAmt"])
                        or self._leverage(account_row.get("leverage")) != self._leverage(risk_row.get("leverage"))):
                    raise TradingError("账户余额与持仓快照正在同步，稍后重试")
                margin_type = risk_row.get("marginType")
                if (not isinstance(margin_type, str) or margin_type.lower() not in ("cross", "crossed", "isolated")
                        or account_row.get("isolated") != (margin_type.lower() == "isolated")):
                    raise TradingError("账户与持仓的保证金模式尚未同步，稍后重试")
            risk_unrealized = sum((Fraction(dec(row.get("unRealizedProfit")))
                                   for key, row in risk.items() if key in relevant), Fraction(0))

        positions = []
        for key, row in relevant.items():
            symbol, side = key
            qty = dec(row["positionAmt"]).copy_abs()
            risk_row = risk.get(key)
            mark = marks[symbol]
            if mark is None and risk_row is not None:
                mark = positive(risk_row.get("markPrice"))
            if mark is None:
                if qty:
                    raise TradingError("账户持仓缺少有效标记价格")
                # Only a selected flat symbol omitted by risk reaches the
                # existing public quote fallback, once for its two flat legs.
                book = self.market.book(symbol)
                book.require_fresh()
                mark = marks[symbol] = positive(book.mark)
            liquidation = None
            if risk_row is not None and risk_row.get("liquidationPrice") is not None:
                liquidation = positive(risk_row["liquidationPrice"], True)
            positions.append(Position(symbol, side, qty, positive(row["entryPrice"], allow_zero=not qty),
                mark, leverages[symbol], dec(row["unrealizedProfit"]), liquidation, isolated=row["isolated"]))
        return positions, rows, leverages, risk_unrealized

    @staticmethod
    def _cycle_current_caps(symbols, rows, leverages, read):
        caps = {}
        for symbol in symbols:
            pair = [rows[(symbol, side)] for side in ("LONG", "SHORT")]
            try:
                # Validate every reported value, even if the other leg is missing it.
                reported = [positive(row["maxNotional"], True) for row in pair if "maxNotional" in row]
                if len(reported) == 2:
                    cap = min(reported)
                else:
                    data = read("bracket:" + symbol, "/fapi/v3/leverageBracket", {"symbol": symbol}, ttl=5)
                    if isinstance(data, list):
                        if any(not isinstance(item, dict) for item in data):
                            raise TradingError("账户风控档位响应无效")
                        matches = [item for item in data if item.get("symbol") == symbol]
                        if len(matches) != 1:
                            raise TradingError("账户风控档位缺失或重复")
                        data = matches[0]
                    if not isinstance(data, dict) or data.get("symbol") != symbol:
                        raise TradingError("账户风控档位交易代码不匹配")
                    try:
                        if not isinstance(data.get("brackets"), list) or any(not isinstance(tier, dict) for tier in data["brackets"]):
                            raise TradingError("账户风控档位响应无效")
                        tiers = validate_brackets(data["brackets"])
                    except (KeyError, TypeError):
                        raise TradingError("账户风控档位响应无效") from None
                    cap = min([leverage_cap(tiers, leverages[symbol])] + reported)
            except TradingError:
                # Missing opening capacity must not strand a held increment.
                # No cap is published, so planning new exposure still fails.
                if any(dec(row["positionAmt"]) for row in pair):
                    continue
                raise
            caps[symbol] = (leverages[symbol], cap)
        return caps

    def cycle_snapshot(self, symbols, fresh_modes=False):
        with self._snapshot_lock:
            return self._cycle_snapshot(symbols, fresh_modes=fresh_modes)

    def _cycle_snapshot(self, symbols, fresh_modes=False):
        """Join independent cycle GETs before validating one complete snapshot.

        Aster V3 keeps the latest 100 unique nonces per signer, allowing a small
        out-of-order group; API's nonce lock preserves uniqueness. HTTPX Client
        supports sharing across threads. Only GETs use this bounded local pool.
        """
        symbols = tuple(dict.fromkeys(symbols))
        self.leverage_snapshot = None
        started = time.time()
        if fresh_modes:
            self.cached_at.pop("multi", None)
        specs = [("multi", "/fapi/v3/multiAssetsMargin", None, 15, 30),
                 ("account", "/fapi/v3/accountWithJoinMargin", None, None, 5)]
        values, fetched, pending = {}, {}, []
        for key, path, params, ttl, weight in specs:
            stamp = self.cached_at.get(key, -1e9)
            if ttl is not None and time.monotonic() - stamp < ttl:
                values[key] = self.cached[key]
            else:
                pending.append((key, path, params, ttl, weight))
        budget = getattr(self.api, "budget", None)
        flags = budget._priority_flags() if isinstance(budget, RateBudget) else None

        def fetch(spec):
            key, path, params, ttl, weight = spec
            context = budget._inherit_priority(flags) if flags is not None else nullcontext()
            with context:
                stamp = time.monotonic()
                value = self.api.call("GET", path, params, signed=True, weight=weight)
                return value, stamp

        errors = []
        # Exiting the pool waits even on failure: no private read can outlive its
        # account work or overlap the next verification/submit round.
        with ThreadPoolExecutor(max_workers=min(6, len(pending)), thread_name_prefix="cycle-read") as pool:
            futures = [(spec, pool.submit(fetch, spec)) for spec in pending]
            for spec, future in futures:
                try:
                    value, stamp = future.result()
                    values[spec[0]] = value
                    if spec[3] is not None:
                        fetched[spec[0]] = (value, stamp)
                except BaseException as exc:
                    errors.append(exc)
        if errors:
            def priority(exc):
                if not isinstance(exc, Exception):
                    return (5, 0)
                if isinstance(exc, AccountModeError):
                    return (4, 0)
                if isinstance(exc, ExchangeError) and not isinstance(exc, RequestNotSent):
                    try:
                        delay = float(exc.retry_after)
                    except (TypeError, ValueError, OverflowError):
                        delay = 0
                    return (3, delay if math.isfinite(delay) else 0)
                return (1 if isinstance(exc, RequestNotSent) else 2, 0)
            raise max(errors, key=priority)

        def read(key, path, params=None, *, ttl=None, weight=1):
            stamp = fetched[key][1] if key in fetched else self.cached_at.get(key, -1e9)
            if key not in values and ttl is not None and time.monotonic() - stamp < ttl:
                values[key] = self.cached[key]
            if key not in values or (ttl is not None and time.monotonic() - stamp >= ttl):
                value, stamp = fetch((key, path, params, ttl, weight))
                values[key] = value
                if ttl is not None:
                    fetched[key] = value, stamp
            if ttl is not None and time.monotonic() - stamp >= ttl:
                raise TradingError("账户模式或风控档位查询已过期，等待重试")
            return values[key]

        try:
            account = values["account"]
            asset = self._account_asset(account)
            positions, rows, leverages, risk_unrealized = self._cycle_positions(account, symbols, read)
            caps = self._cycle_current_caps(symbols, rows, leverages, read)
            multi = read("multi", "/fapi/v3/multiAssetsMargin", ttl=15, weight=30)
            if not isinstance(multi, dict) or type(multi.get("multiAssetsMargin")) is not bool:
                raise TradingError("账户保证金模式响应无效")
            snapshot = self._account_snapshot(account, asset, positions, symbols, hedge=True,
                multi=multi["multiAssetsMargin"], started=started, current_caps=caps, risk_unrealized=risk_unrealized)
            # A fallback tier keeps its original five-second lifetime after it
            # becomes a current-leverage cap, including the final submit check.
            snapshot.cycle_cap_cached_at = {
                symbol: (fetched["bracket:" + symbol][1] if "bracket:" + symbol in fetched
                         else self.cached_at["bracket:" + symbol])
                for symbol in caps if "bracket:" + symbol in values
            }
            snapshot.require_fresh()
            for key in caps:
                cache_key = "bracket:" + key
                if cache_key in values:
                    stamp = fetched[cache_key][1] if cache_key in fetched else self.cached_at[cache_key]
                    if time.monotonic() - stamp >= 5:
                        raise TradingError("账户风控档位查询已过期，等待重试")
            # Failed rounds publish neither partially refreshed caches nor an
            # authorization token; cache age includes signing and network time.
            for key, (value, stamp) in fetched.items():
                self.cached[key], self.cached_at[key] = value, stamp
            if fresh_modes:
                self.leverage_snapshot = (snapshot, time.monotonic())
            return snapshot
        except BaseException:
            self.leverage_snapshot = None
            raise

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
            return self._cycle_write("POST", "/fapi/v3/leverage", {"symbol": symbol, "leverage": str(leverage)}, refresh_modes=True)
        except ExchangeError as exc:
            status_rejected = exc.http_status in LEVERAGE_REJECTION_HTTP_STATUSES and exc.code not in (-1006, -1007)
            code_rejected = not isinstance(exc, AmbiguousOrder) and exc.code in LEVERAGE_REJECTION_CODES
            if not isinstance(exc, RequestNotSent) and (status_rejected or code_rejected):
                raise LeverageRejected(str(exc), code=exc.code, retry_after=max(30, exc.retry_after),
                                       http_status=exc.http_status) from None
            raise

    def set_cycle_leverage(self, symbol, leverage, *, checked_snapshot=None, before_submit=None):
        """The independent cycle may choose any integer leverage, only while flat."""
        if type(leverage) is not int or not 1 <= leverage <= 125:
            raise TradingError("独立循环杠杆必须为 1 至 125 的整数")
        # The executor protects its own in-flight orders; verify actual flat
        # positions without querying external open orders in this taker cycle.
        self.leverage_snapshot = None
        try:
            snapshot = self.cycle_snapshot([symbol], fresh_modes=True)
            snapshot.require_modes([symbol])
            long, short = snapshot.require_ready(symbol)
            if long.qty or short.qty:
                raise TradingError("独立循环仅允许在所选品种确认空仓时设置杠杆")
            if before_submit is not None:
                before_submit(snapshot)
                long, short = snapshot.require_ready(symbol)
                if long.qty or short.qty:
                    raise TradingError("独立循环杠杆提交前必须仍为空仓")
            if leverage == long.leverage:
                return {"symbol": symbol, "leverage": leverage}
        except RequestNotSent:
            raise
        except TradingError as exc:
            raise RequestNotSent(str(exc), retry_after=getattr(exc, "retry_after", 0)) from None
        try:
            return self._cycle_write("POST", "/fapi/v3/leverage", {"symbol": symbol, "leverage": str(leverage)}, refresh_modes=True)
        except ExchangeError as exc:
            status_rejected = exc.http_status in LEVERAGE_REJECTION_HTTP_STATUSES and exc.code not in (-1006, -1007)
            code_rejected = not isinstance(exc, AmbiguousOrder) and exc.code in LEVERAGE_REJECTION_CODES
            if not isinstance(exc, RequestNotSent) and (status_rejected or code_rejected):
                raise LeverageRejected(str(exc), code=exc.code, retry_after=max(30, exc.retry_after),
                                       http_status=exc.http_status) from None
            raise

    def cycle_trades(self, order, receipt, created_at, *, checkpoint=None):
        """Fetch every fill of one verified order, keeping exchange UTC timestamps.

        userTrades has no orderId filter. Its time filters cannot be combined
        with fromId, so only the initial page uses a bounded time window.
        """
        if any(receipt.get(key) != order[key] for key in ("symbol", "positionSide", "side")) \
                or receipt.get("clientOrderId") != order["newClientOrderId"]:
            raise TradingError("循环成交查询的订单回执身份不一致")
        expected = Fraction(positive(receipt.get("executedQty"), True))
        if expected > Fraction(positive(order["quantity"])):
            raise TradingError("循环成交查询数量超过委托数量")
        if not expected:
            return []

        def integer(value, name):
            if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isdigit() or len(str(value)) > 40:
                raise TradingError(f"循环成交{name}无效")
            return int(value)

        oid = str(integer(receipt.get("orderId"), "订单编号"))
        cid, symbol = order["newClientOrderId"], order["symbol"]
        if type(created_at) not in (int, float) or not math.isfinite(created_at) or created_at <= 0:
            raise TradingError("循环订单创建时间无效，无法核对成交")
        state = checkpoint if checkpoint is not None else self.cycle_trade_queries.setdefault(cid, {})
        fingerprint = [oid, wire(expected), symbol, order["positionSide"], order["side"]]
        if state.get("identity") != fingerprint:
            # An exchange order timestamp is a tighter lookup anchor than the
            # local write time, especially after delayed restart recovery.
            order_ms = integer(receipt["time"], "订单时间") if receipt.get("time") is not None else int(created_at * 1000)
            end_ms = integer(receipt["updateTime"], "回执时间") if receipt.get("updateTime") is not None else int(time.time() * 1000)
            begin_ms = max(0, order_ms - 60000)
            end_ms = max(order_ms, end_ms) + 60000
            state.clear()
            state.update(identity=fingerprint, begin_ms=begin_ms, end_ms=end_ms, window_start=begin_ms,
                         window_end=min(end_ms, begin_ms + 7 * 86400000 - 1), from_id=None, fills={})
        # Bound one scheduler pass; persisted checkpoints resume pagination on
        # the next tick without spending the repair reserve on repeated pages.
        for _ in range(5):
            cursor = state.get("from_id")
            params = {"symbol": symbol, "limit": 1000}
            if cursor is None:
                params.update(startTime=state["window_start"], endTime=state["window_end"])
            else:
                params["fromId"] = cursor
            rows = self.api.call("GET", "/fapi/v3/userTrades", params, signed=True, weight=5)
            if not isinstance(rows, list) or len(rows) > 1000:
                raise TradingError("循环逐笔成交响应无效")
            ids = []
            for row in rows:
                if not isinstance(row, dict) or row.get("symbol") != symbol:
                    raise TradingError("循环逐笔成交品种不一致")
                trade_id = integer(row.get("id"), "编号")
                row_oid = str(integer(row.get("orderId"), "订单编号"))
                ids.append(trade_id)
                if cursor is not None and trade_id < cursor:
                    raise TradingError("循环逐笔成交分页没有前进，等待重新核对")
                if row_oid != oid:
                    continue
                if row.get("side") != order["side"] or row.get("positionSide") != order["positionSide"]:
                    raise TradingError("循环逐笔成交方向与委托不一致")
                stamp = integer(row.get("time"), "时间")
                if not 0 < stamp <= 253402300799999:
                    raise TradingError("循环逐笔成交时间无效")
                qty, price = positive(row.get("qty")), positive(row.get("price"))
                fill = {"trade_id": str(trade_id), "order_id": oid, "client_id": cid, "symbol": symbol,
                        "position_side": order["positionSide"], "side": order["side"],
                        "quantity": wire(qty), "price": wire(price), "notional": wire(Fraction(qty) * Fraction(price)),
                        "executed_at": stamp / 1000, "time_source": "exchange"}
                previous = state["fills"].get(str(trade_id))
                if previous is not None and previous != fill:
                    raise TradingError("同一循环成交编号出现冲突明细")
                state["fills"][str(trade_id)] = fill
            matched = sum((Fraction(dec(fill["quantity"])) for fill in state["fills"].values()), Fraction(0))
            if matched > expected:
                raise TradingError("逐笔成交合计超过已核实回执数量")
            if matched == expected:
                result = sorted(state["fills"].values(), key=lambda fill: (fill["executed_at"], int(fill["trade_id"])))
                state.clear()
                return result
            if len(rows) == 1000:
                state["from_id"] = max(ids) + 1
                continue
            if cursor is None and state["window_end"] < state["end_ms"]:
                state["window_start"] = state["window_end"] + 1
                state["window_end"] = min(state["end_ms"], state["window_start"] + 7 * 86400000 - 1)
                continue
            # Fills may become visible after order status. Restart the lookup
            # window next time, retain known fills, and never call a short page 0.
            state.update(from_id=None, window_start=state["begin_ms"],
                         window_end=min(state["end_ms"], state["begin_ms"] + 7 * 86400000 - 1))
            raise TradingError("循环逐笔成交尚未查全，等待补账后再开新仓")
        raise TradingError("循环逐笔成交正在分页补账，等待后续核对")

    def submit(self, orders):
        self.leverage_snapshot = None
        if len(orders) == 1:
            return [self._cycle_write("POST", "/fapi/v3/order", orders[0])]
        return self._cycle_write("POST", "/fapi/v3/batchOrders", {"batchOrders": json.dumps(orders, separators=(",", ":"))}, weight=5)

    def query(self, symbol, client_id):
        with self.reconciliation_budget():
            return self.api.call("GET", "/fapi/v3/order", {"symbol": symbol, "origClientOrderId": client_id}, signed=True)

    def cancel(self, symbol, client_id):
        with self.reconciliation_budget():
            return self._cycle_write("DELETE", "/fapi/v3/order", {"symbol": symbol, "origClientOrderId": client_id})

    def close(self):
        with self._cycle_lifecycle_lock:
            self._cycle_hot_closed = True
        try:
            self.stop_cycle_hot_data()
        finally:
            self.api.close()
