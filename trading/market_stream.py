"""Public market data cache. The receiver never makes trading or REST calls."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import json
import logging
import re
import threading
import time

from websockets.sync.client import connect as websocket_connect

from .models import Book, SYMBOLS, TradingError, positive


_LOG = logging.getLogger(__name__)
_BASE_URL = "wss://fstream.asterdex.com/stream?streams="
_MAX_INTEGER = 2**63 - 1


def _integer(value):
    if type(value) is not int or not 0 < value <= _MAX_INTEGER:
        raise ValueError("Invalid market event integer")
    return value


@dataclass(frozen=True)
class _BBO:
    bid: Decimal
    ask: Decimal
    bid_qty: Decimal
    ask_qty: Decimal
    event_ms: int
    transaction_ms: int
    update_id: int
    expires: float


@dataclass(frozen=True)
class _Mark:
    price: Decimal
    event_ms: int
    expires: float


class PublicQuoteStream:
    """One combined connection, with fresh BBO and mark required independently.

    Construction and ``book`` are network-free. ``start`` starts a single daemon
    receiver; ``close`` permanently stops the instance and is safe to repeat.
    ``connect``, ``clock`` and ``monotonic`` may be injected for offline tests.
    """

    _OPEN_TIMEOUT = 3
    _CLOSE_TIMEOUT = 1
    _RECV_TIMEOUT = 0.25
    _RETRY_INITIAL = 0.5
    _RETRY_MAX = 30
    _STABLE_SECONDS = 30
    _MAX_AGE = 3
    _FUTURE_ALLOWANCE = 1

    def __init__(self, symbols=SYMBOLS, *, connect=None, clock=None, monotonic=None):
        self.symbols = tuple(dict.fromkeys(symbols))
        if not self.symbols or len(self.symbols) > 100 or any(
            not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{1,32}", symbol)
            for symbol in self.symbols
        ):
            raise ValueError("Invalid public quote symbols")
        self._streams = {
            f"{symbol.lower()}@{suffix}": (symbol, kind)
            for symbol in self.symbols
            for suffix, kind in (("bookTicker", "bbo"), ("markPrice@1s", "mark"))
        }
        self.url = _BASE_URL + "/".join(self._streams)
        self._connect = connect or websocket_connect
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._connected = False
        self._bbo = {}
        self._mark = {}
        # Invalid data removes a quote but cannot reset its ordering watermark.
        self._bbo_order = {}
        self._mark_order = {}

    def _now(self):
        return time.time() if self._clock is None else self._clock()

    def _ticks(self):
        return time.monotonic() if self._monotonic is None else self._monotonic()

    def start(self):
        with self._lock:
            if self._stop.is_set() or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="public-quotes", daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                raise

    def _clear_locked(self):
        self._bbo.clear()
        self._mark.clear()
        self._bbo_order.clear()
        self._mark_order.clear()

    def close(self):
        self._stop.set()
        with self._lock:
            self._connected = False
            self._clear_locked()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            # The receiver owns close(), so a caller never blocks in a socket
            # handshake. A connect still in progress also observes _stop.
            thread.join(self._OPEN_TIMEOUT + self._CLOSE_TIMEOUT + self._RECV_TIMEOUT + 0.5)

    def book(self, symbol):
        with self._lock:
            if not self._connected or self._stop.is_set():
                return None
            bbo, mark = self._bbo.get(symbol), self._mark.get(symbol)
            if bbo is None or mark is None:
                return None
            now, ticks = self._now(), self._ticks()
            if not self._fresh(bbo.event_ms, now) or not self._fresh(bbo.transaction_ms, now) or ticks > bbo.expires:
                self._bbo.pop(symbol, None)
                return None
            if not self._fresh(mark.event_ms, now) or ticks > mark.expires:
                self._mark.pop(symbol, None)
                return None
            book = Book(bbo.bid, bbo.ask, bbo.bid_qty, bbo.ask_qty, mark.price,
                        min(bbo.event_ms, bbo.transaction_ms, mark.event_ms) / 1000)
            book.require_fresh(now, self._MAX_AGE)
            return book

    def _fresh(self, event_ms, now):
        return -self._FUTURE_ALLOWANCE <= now - event_ms / 1000 <= self._MAX_AGE

    def _handle_message(self, message):
        """Validate one combined event and atomically replace only its own side."""
        try:
            envelope = json.loads(message)
        except (TypeError, ValueError, UnicodeError):
            with self._lock:
                self._bbo.clear()
                self._mark.clear()
            return
        if not isinstance(envelope, dict):
            with self._lock:
                self._bbo.clear()
                self._mark.clear()
            return
        stream = envelope.get("stream")
        if not isinstance(stream, str) or stream not in self._streams:
            # Control messages and unrelated events cannot freshen any quote.
            return
        symbol, kind = self._streams[stream]
        with self._lock:
            if not self._connected or self._stop.is_set():
                return
            cache = self._bbo if kind == "bbo" else self._mark
            try:
                data = envelope["data"]
                expected = "bookTicker" if kind == "bbo" else "markPriceUpdate"
                if not isinstance(data, dict) or data.get("s") != symbol or data.get("e") != expected:
                    raise ValueError("Mismatched market event")
                event_ms = _integer(data["E"])
                if kind == "bbo":
                    transaction_ms, update_id = _integer(data["T"]), _integer(data["u"])
                    order = (event_ms, transaction_ms, update_id)
                    previous = self._bbo_order.get(symbol)
                    if previous is not None and (event_ms < previous[0] or transaction_ms < previous[1] or update_id <= previous[2]):
                        return
                elif event_ms <= self._mark_order.get(symbol, 0):
                    return
                # A delayed event cannot replace the accepted quote, even if
                # its old price fields are malformed or its time has expired.
                now, ticks = self._now(), self._ticks()
                if not self._fresh(event_ms, now):
                    raise ValueError("Stale market event")
                if kind == "bbo":
                    if not self._fresh(transaction_ms, now):
                        raise ValueError("Stale book transaction")
                    bid, ask, bid_qty, ask_qty = (positive(data[key]) for key in ("b", "a", "B", "A"))
                    if ask < bid:
                        raise ValueError("Crossed book")
                    # Source age consumes the monotonic lifetime immediately;
                    # moving the wall clock backwards cannot extend the quote.
                    expires = ticks + self._MAX_AGE - (now - min(event_ms, transaction_ms) / 1000)
                    cache[symbol] = _BBO(bid, ask, bid_qty, ask_qty, *order, expires)
                    self._bbo_order[symbol] = order
                else:
                    price = positive(data["p"])
                    expires = ticks + self._MAX_AGE - (now - event_ms / 1000)
                    cache[symbol] = _Mark(price, event_ms, expires)
                    self._mark_order[symbol] = event_ms
            except (KeyError, TypeError, ValueError, TradingError):
                cache.pop(symbol, None)

    def _run(self):
        retry = self._RETRY_INITIAL
        while not self._stop.is_set():
            connection = None
            connected_at = None
            try:
                connection = self._connect(
                    self.url, open_timeout=self._OPEN_TIMEOUT,
                    close_timeout=self._CLOSE_TIMEOUT, ping_interval=20,
                    ping_timeout=20, max_size=65536, max_queue=16,
                    compression=None,
                )
                with self._lock:
                    if self._stop.is_set():
                        return
                    self._clear_locked()
                    self._connected = True
                    connected_at = self._ticks()
                while not self._stop.is_set():
                    try:
                        message = connection.recv(timeout=self._RECV_TIMEOUT)
                    except TimeoutError:
                        continue
                    self._handle_message(message)
            except Exception as exc:
                _LOG.debug("Public quote stream disconnected (%s)", type(exc).__name__)
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
