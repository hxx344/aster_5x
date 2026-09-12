"""Sequence-checked public depth cache; REST seeding belongs to the caller."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from fractions import Fraction
import json
import logging
import math
import re
import threading
import time

from websockets.sync.client import connect as websocket_connect

from .depth import DEPTH_LIMIT, DEPTH_MAX_AGE, DepthSnapshot
from .models import SYMBOLS, TradingError, positive


_LOG = logging.getLogger(__name__)
_BASE_URL = "wss://fstream.asterdex.com/stream?streams="
_MAX_INTEGER = 2**63 - 1


def _integer(value, *, zero=False):
    if type(value) is not int or not (0 if zero else 1) <= value <= _MAX_INTEGER:
        raise ValueError("Invalid depth event integer")
    return value


def _updates(rows, limit):
    if not isinstance(rows, list) or len(rows) > limit:
        raise ValueError("Invalid depth update levels")
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, list) or len(row) != 2:
            raise ValueError("Invalid depth update level")
        price, quantity = Fraction(positive(row[0])), Fraction(positive(row[1], True))
        if price in seen:
            raise ValueError("Duplicate depth update price")
        seen.add(price)
        result.append((price, quantity))
    return tuple(result)


@dataclass(frozen=True)
class _SeedToken:
    owner: object
    symbol: str
    connection: int
    generation: int


@dataclass(frozen=True)
class _Update:
    first: int
    last: int
    previous: int
    event_ms: int
    transaction_ms: int
    bids: tuple
    asks: tuple
    received_at: float
    received_ticks: float


@dataclass
class _DepthState:
    token: _SeedToken
    valid: threading.Event = field(default_factory=threading.Event)
    pending: deque = field(default_factory=deque)
    pending_levels: int = 0
    seed_id: int | None = None
    seed_ticks: float | None = None
    last: int | None = None
    event_ms: int | None = None
    transaction_ms: int | None = None
    bids: dict = field(default_factory=dict)
    asks: dict = field(default_factory=dict)
    bid_floor: Fraction | None = None
    ask_ceiling: Fraction | None = None
    timestamp: float = 0
    monotonic_timestamp: float = 0


class PublicDepthStream:
    """One combined diff-depth connection, initialized by external REST reads.

    ``seed_token`` is a non-claiming token, available after the first buffered
    event: callers serialize REST requests per symbol. ``seed`` returning True
    means accepted, not necessarily bridged;
    only ``snapshot`` exposes a synchronized book. A seed waiting over ten
    seconds for its bridge is discarded on the next read/event. A synchronized
    but quiet book remains synchronized and does not request another REST seed.
    """

    _OPEN_TIMEOUT = 3
    _CLOSE_TIMEOUT = 1
    _RECV_TIMEOUT = 0.25
    _RETRY_INITIAL = 0.5
    _RETRY_MAX = 30
    _STABLE_SECONDS = 30
    _BRIDGE_TIMEOUT = 10
    _FUTURE_ALLOWANCE = 1
    _MAX_MESSAGE_BYTES = 262144
    _MAX_BUFFER_EVENTS = 256
    _MAX_BUFFER_LEVELS = 8192
    _MAX_UPDATE_LEVELS = 2000
    _MAX_LEVELS = DEPTH_LIMIT

    def __init__(self, symbols=SYMBOLS, *, connect=None, clock=None, monotonic=None):
        self.symbols = tuple(dict.fromkeys(symbols))
        if not self.symbols or len(self.symbols) > 100 or any(
            not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{1,32}", symbol)
            for symbol in self.symbols
        ):
            raise ValueError("Invalid public depth symbols")
        self._streams = {f"{symbol.lower()}@depth@100ms": symbol for symbol in self.symbols}
        self.url = _BASE_URL + "/".join(self._streams)
        self._connect = connect or websocket_connect
        self._clock, self._monotonic = clock, monotonic
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._connected = False
        self._owner = object()
        self._connection = 0
        self._generation = 0
        self._states = {}

    def _now(self):
        return time.time() if self._clock is None else self._clock()

    def _ticks(self):
        return time.monotonic() if self._monotonic is None else self._monotonic()

    def _invalidate_locked(self, symbol):
        previous = self._states.get(symbol)
        if previous is not None:
            previous.valid.clear()
        self._generation += 1
        state = _DepthState(_SeedToken(self._owner, symbol, self._connection, self._generation))
        self._states[symbol] = state
        return state

    def _clear_locked(self):
        for state in self._states.values():
            state.valid.clear()
        self._states.clear()

    def _state_locked(self, symbol):
        state = self._states.get(symbol)
        if state is None:
            state = self._invalidate_locked(symbol)
        if state.seed_ticks is not None and not state.valid.is_set() and self._ticks() - state.seed_ticks > self._BRIDGE_TIMEOUT:
            state = self._invalidate_locked(symbol)
        return state

    def start(self):
        with self._lock:
            if self._stop.is_set() or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="public-depth", daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                raise

    def close(self):
        self._stop.set()
        with self._lock:
            self._connected = False
            self._clear_locked()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(self._OPEN_TIMEOUT + self._CLOSE_TIMEOUT + self._RECV_TIMEOUT + 0.5)

    def seed_token(self, symbol):
        with self._lock:
            if symbol not in self.symbols or not self._connected or self._stop.is_set():
                return None
            state = self._state_locked(symbol)
            return state.token if state.seed_id is None and state.pending else None

    def snapshot(self, symbol):
        with self._lock:
            if symbol not in self.symbols or not self._connected or self._stop.is_set():
                return None
            state = self._state_locked(symbol)
            if not state.valid.is_set():
                return None
            return DepthSnapshot(
                tuple(sorted(state.bids.items(), reverse=True)), tuple(sorted(state.asks.items())),
                state.timestamp, monotonic_timestamp=state.monotonic_timestamp,
                validity=state.valid.is_set,
            )

    def seed(self, symbol, raw_response, *, token, requested_at):
        with self._lock:
            if symbol not in self.symbols or not self._connected or self._stop.is_set():
                return False
            state = self._state_locked(symbol)
            if token != state.token or state.seed_id is not None:
                return False
            try:
                now = self._now()
                if type(requested_at) not in (int, float) or not math.isfinite(requested_at) or requested_at > now + self._FUTURE_ALLOWANCE:
                    raise ValueError("Invalid depth request time")
                if not isinstance(raw_response, dict) or raw_response.get("symbol", symbol) != symbol:
                    raise ValueError("Mismatched depth response")
                seed_id = _integer(raw_response.get("lastUpdateId"))
                snapshot = DepthSnapshot.from_response(raw_response, requested_at=requested_at, now=now)
                state.bids, state.asks = dict(snapshot.bids), dict(snapshot.asks)
                state.bid_floor = min(state.bids, default=None)
                state.ask_ceiling = max(state.asks, default=None)
                self._bound_levels(state)
                state.seed_id, state.seed_ticks = seed_id, self._ticks()
                state.timestamp = snapshot.timestamp
                state.monotonic_timestamp = state.seed_ticks - max(0, now - snapshot.timestamp)
                pending = tuple(state.pending)
                state.pending.clear()
                state.pending_levels = 0
                for update in pending:
                    if not self._apply_locked(state, update):
                        raise ValueError("Depth snapshot cannot bridge buffered updates")
                return True
            except (KeyError, TypeError, ValueError, OverflowError, TradingError):
                self._invalidate_locked(symbol)
                return False

    def _bound_levels(self, state):
        # Shrinking a known boundary is conservative; it can never expose an
        # unknown gap beyond the original REST snapshot's coverage.
        if len(state.bids) > self._MAX_LEVELS:
            state.bids = dict(sorted(state.bids.items(), reverse=True)[:self._MAX_LEVELS])
            state.bid_floor = min(state.bids)
        if len(state.asks) > self._MAX_LEVELS:
            state.asks = dict(sorted(state.asks.items())[:self._MAX_LEVELS])
            state.ask_ceiling = max(state.asks)

    def _apply_locked(self, state, update):
        if state.valid.is_set():
            if update.last <= state.last:
                return True
            if update.previous != state.last:
                return False
            if update.event_ms < state.event_ms or update.transaction_ms < state.transaction_ms:
                return False
        else:
            if update.last < state.seed_id:
                return True
            if not update.first <= state.seed_id <= update.last:
                return False
        if (state.bid_floor is None and any(quantity for _, quantity in update.bids)
                or state.ask_ceiling is None and any(quantity for _, quantity in update.asks)):
            # A side absent from the seed has no established coverage boundary.
            # Its first liquidity needs a new snapshot before it can be used.
            return False
        for price, quantity in update.bids:
            if state.bid_floor is not None and price >= state.bid_floor:
                if quantity:
                    state.bids[price] = quantity
                else:
                    state.bids.pop(price, None)
        for price, quantity in update.asks:
            if state.ask_ceiling is not None and price <= state.ask_ceiling:
                if quantity:
                    state.asks[price] = quantity
                else:
                    state.asks.pop(price, None)
        self._bound_levels(state)
        if (state.bid_floor is not None and not state.bids
                or state.ask_ceiling is not None and not state.asks):
            # Prices may have moved entirely outside the known REST coverage.
            # Rebuild rather than remaining a permanently empty synchronized book.
            return False
        if state.bids and state.asks and max(state.bids) > min(state.asks):
            return False
        state.last = update.last
        state.event_ms, state.transaction_ms = update.event_ms, update.transaction_ms
        state.timestamp = min(update.event_ms / 1000, update.received_at)
        state.monotonic_timestamp = update.received_ticks - max(0, update.received_at - state.timestamp)
        state.valid.set()
        return True

    def _buffer_locked(self, symbol, state, update):
        if state.pending:
            previous = state.pending[-1]
            if update.last <= previous.last:
                return
            if update.previous != previous.last or update.event_ms < previous.event_ms or update.transaction_ms < previous.transaction_ms:
                state = self._invalidate_locked(symbol)
        count = len(update.bids) + len(update.asks)
        if len(state.pending) >= self._MAX_BUFFER_EVENTS or state.pending_levels + count > self._MAX_BUFFER_LEVELS:
            # The discarded prefix may contain the bridge. Any in-flight seed
            # must therefore be rejected, even if the latest event is retained.
            state = self._invalidate_locked(symbol)
        if count <= self._MAX_BUFFER_LEVELS:
            state.pending.append(update)
            state.pending_levels += count

    def _handle_message(self, message):
        try:
            if not isinstance(message, (str, bytes)) or len(message) > self._MAX_MESSAGE_BYTES:
                raise ValueError("Oversized depth message")
            envelope = json.loads(message)
            if not isinstance(envelope, dict):
                raise ValueError("Invalid depth envelope")
        except (TypeError, ValueError, UnicodeError):
            with self._lock:
                for symbol in self.symbols:
                    self._invalidate_locked(symbol)
            return
        stream = envelope.get("stream")
        if not isinstance(stream, str) or stream not in self._streams:
            return
        symbol = self._streams[stream]
        with self._lock:
            if not self._connected or self._stop.is_set():
                return
            state = self._state_locked(symbol)
            try:
                data = envelope["data"]
                if not isinstance(data, dict) or data.get("s") != symbol or data.get("e") != "depthUpdate":
                    raise ValueError("Mismatched depth event")
                first, last = _integer(data["U"]), _integer(data["u"])
                previous = _integer(data["pu"], zero=True)
                if first > last or previous >= last:
                    raise ValueError("Invalid depth update range")
                watermark = state.last if state.valid.is_set() else (state.pending[-1].last if state.pending else None)
                if watermark is not None and last <= watermark:
                    return
                event_ms, transaction_ms = _integer(data["E"]), _integer(data["T"])
                now, ticks = self._now(), self._ticks()
                if not -self._FUTURE_ALLOWANCE <= now - event_ms / 1000 <= DEPTH_MAX_AGE:
                    raise ValueError("Stale depth event")
                if transaction_ms > event_ms + self._FUTURE_ALLOWANCE * 1000:
                    raise ValueError("Invalid depth transaction time")
                update = _Update(first, last, previous, event_ms, transaction_ms,
                                 _updates(data["b"], self._MAX_UPDATE_LEVELS),
                                 _updates(data["a"], self._MAX_UPDATE_LEVELS), now, ticks)
                if state.seed_id is None:
                    self._buffer_locked(symbol, state, update)
                elif not self._apply_locked(state, update):
                    self._invalidate_locked(symbol)
            except (KeyError, TypeError, ValueError, OverflowError, TradingError):
                self._invalidate_locked(symbol)

    def _run(self):
        retry = self._RETRY_INITIAL
        while not self._stop.is_set():
            connection, connected_at = None, None
            try:
                connection = self._connect(
                    self.url, open_timeout=self._OPEN_TIMEOUT,
                    close_timeout=self._CLOSE_TIMEOUT, ping_interval=20,
                    ping_timeout=20, max_size=self._MAX_MESSAGE_BYTES, max_queue=16,
                    compression=None,
                )
                with self._lock:
                    if self._stop.is_set():
                        return
                    self._clear_locked()
                    self._connection += 1
                    self._connected = True
                    connected_at = self._ticks()
                while not self._stop.is_set():
                    try:
                        message = connection.recv(timeout=self._RECV_TIMEOUT)
                    except TimeoutError:
                        continue
                    self._handle_message(message)
            except Exception as exc:
                _LOG.debug("Public depth stream disconnected (%s)", type(exc).__name__)
            finally:
                with self._lock:
                    self._connected = False
                    self._clear_locked()
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
            if connected_at is not None and self._ticks() - connected_at >= self._STABLE_SECONDS:
                retry = self._RETRY_INITIAL
            if self._stop.wait(retry):
                return
            retry = min(retry * 2, self._RETRY_MAX)
