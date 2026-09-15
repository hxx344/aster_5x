"""Revocable, network-free leases over background account snapshots."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import logging
import math
import re
import threading
import time

from .models import TradingError


_LOG = logging.getLogger(__name__)


class HotAccountUnavailable(TradingError):
    def __init__(self, message="账户热数据尚未就绪", retry_after=1):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class _RefreshTicket:
    symbols: tuple[str, ...]
    refresh_modes: bool
    epoch: int


@dataclass(frozen=True)
class _Published:
    snapshot: object
    started_monotonic: float
    valid_until_monotonic: float | None
    version: int


class CycleAccountLease:
    """A mutable working copy whose original authority stays in its owner."""

    def __init__(self, owner, published):
        self._owner = owner
        self._published = published
        self.snapshot = deepcopy(published.snapshot)

    @property
    def started_monotonic(self):
        return self._published.started_monotonic

    def require_fresh(self):
        self._owner._require_lease(self)


class CycleAccountCache:
    """REST completion cannot cross an event, disconnect, or configuration change.

    All callbacks are lightweight wake signals, made outside the cache lock.
    Repeated invalidations coalesce until the background worker starts a read.
    """

    _MAX_AGE = 8

    def __init__(self, on_invalidate=None, *, clock=None, monotonic=None):
        if on_invalidate is not None and not callable(on_invalidate):
            raise ValueError("Invalid account cache listener")
        self._listener = on_invalidate
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._symbols = ()
        self._connected = False
        self._epoch = 0
        self._version = 0
        self._published = None
        self._active_ticket = None
        self._refresh_modes = True
        self._pending_notice = False
        self._reason = "账户热数据尚未就绪"

    def _now(self):
        return time.time() if self._clock is None else self._clock()

    def _ticks(self):
        return time.monotonic() if self._monotonic is None else self._monotonic()

    @staticmethod
    def _normalize_symbols(symbols):
        if isinstance(symbols, (str, bytes)):
            raise ValueError("Invalid account cache symbols")
        result = tuple(dict.fromkeys(symbols))
        if len(result) > 100 or any(not isinstance(s, str) or not re.fullmatch(r"[A-Z0-9]{1,32}", s) for s in result):
            raise ValueError("Invalid account cache symbols")
        return result

    def _notice_locked(self):
        if self._pending_notice or self._listener is None:
            return None
        self._pending_notice = True
        return self._listener

    @staticmethod
    def _notify(listener):
        if listener is not None:
            try:
                listener()
            except Exception as exc:
                _LOG.debug("Account cache listener failed (%s)", type(exc).__name__)

    def set_listener(self, on_invalidate=None):
        if on_invalidate is not None and not callable(on_invalidate):
            raise ValueError("Invalid account cache listener")
        with self._lock:
            if self._listener is not on_invalidate:
                self._pending_notice = False
            self._listener = on_invalidate
            listener = self._notice_locked() if self._published is None else None
        self._notify(listener)

    def _invalidate_locked(self, reason, refresh_modes):
        self._epoch += 1
        self._version += 1
        self._published = None
        self._active_ticket = None
        self._refresh_modes |= bool(refresh_modes)
        self._reason = reason
        return self._notice_locked()

    def configure(self, symbols):
        symbols = self._normalize_symbols(symbols)
        with self._lock:
            if symbols == self._symbols:
                return False
            self._symbols = symbols
            listener = self._invalidate_locked("账户热数据配置已更新", True)
        self._notify(listener)
        return True

    def set_connected(self, connected):
        if type(connected) is not bool:
            raise ValueError("Invalid private stream connection state")
        with self._lock:
            self._connected = connected
            listener = self._invalidate_locked("账户热数据需要重新同步" if connected else "账户私有数据流未连接", True)
        self._notify(listener)

    def invalidate(self, reason="账户热数据需要更新", *, refresh_modes=False):
        with self._lock:
            listener = self._invalidate_locked(reason, refresh_modes)
        self._notify(listener)

    def begin_refresh(self):
        with self._lock:
            self._pending_notice = False
            if not self._symbols or not self._connected:
                raise HotAccountUnavailable(self._reason)
            ticket = _RefreshTicket(self._symbols, self._refresh_modes, self._epoch)
            self._active_ticket = ticket
            return ticket

    def _current_ticket_locked(self, ticket):
        return (isinstance(ticket, _RefreshTicket) and self._active_ticket is ticket and self._connected
                and ticket.epoch == self._epoch and ticket.symbols == self._symbols)

    @staticmethod
    def _number(value):
        return type(value) in (int, float) and math.isfinite(value)

    def _require_published_fresh_locked(self, published):
        ticks = self._ticks()
        age = ticks - published.started_monotonic
        if (not self._number(ticks) or not 0 <= age <= self._MAX_AGE
                or (published.valid_until_monotonic is not None and ticks >= published.valid_until_monotonic)):
            raise HotAccountUnavailable("账户热数据已过期")
        try:
            published.snapshot.require_fresh(self._now())
        except (TradingError, TypeError, ValueError, AttributeError):
            raise HotAccountUnavailable("账户热数据已过期") from None

    def publish(self, ticket, snapshot, started_monotonic, *, valid_until_monotonic=None):
        with self._lock:
            if not self._current_ticket_locked(ticket):
                return False
        try:
            if not self._number(started_monotonic):
                raise ValueError("Invalid refresh start")
            if valid_until_monotonic is not None and not self._number(valid_until_monotonic):
                raise ValueError("Invalid refresh deadline")
            saved = deepcopy(snapshot)
        except Exception:
            self.fail(ticket, None)
            return False
        with self._lock:
            if not self._current_ticket_locked(ticket):
                return False
            published = _Published(saved, started_monotonic, valid_until_monotonic, self._version + 1)
            try:
                self._require_published_fresh_locked(published)
            except HotAccountUnavailable:
                listener = self._invalidate_locked("账户热数据刷新结果已过期", False)
                accepted = False
            else:
                self._published = published
                self._version = published.version
                self._active_ticket = None
                self._refresh_modes = False
                self._pending_notice = False
                self._reason = "账户热数据尚未就绪"
                listener = None
                accepted = True
        self._notify(listener)
        return accepted

    def fail(self, ticket, error):
        # Network exception messages may contain signed URLs or listen keys.
        with self._lock:
            if not self._current_ticket_locked(ticket):
                return
            listener = self._invalidate_locked("账户热数据刷新失败，等待重试", False)
        self._notify(listener)

    def _check_current_locked(self, published):
        if not self._connected or published is None or self._published is not published or published.version != self._version:
            raise HotAccountUnavailable(self._reason)
        self._require_published_fresh_locked(published)

    def _expire_locked(self):
        # Expiry revokes readers, but a refresh already in progress may finish.
        self._published = None
        self._version += 1
        self._reason = "账户热数据已过期"
        return self._notice_locked()

    def lease(self, symbols):
        return self._read(symbols, lambda published: CycleAccountLease(self, published))

    def current_leverage(self, symbol):
        """Read scheduling metadata without copying a mutable working snapshot."""
        return self._read([symbol], lambda published: published.snapshot.pair(symbol)[0].leverage)

    def _read(self, symbols, select):
        symbols = self._normalize_symbols(symbols)
        with self._lock:
            if symbols != self._symbols or not symbols:
                raise HotAccountUnavailable("账户热数据配置不匹配")
            try:
                self._check_current_locked(self._published)
            except HotAccountUnavailable as exc:
                listener = self._expire_locked() if self._published is not None else self._notice_locked()
                error = exc
            else:
                return select(self._published)
        self._notify(listener)
        raise error

    def _require_lease(self, lease):
        with self._lock:
            try:
                self._check_current_locked(lease._published)
                lease.snapshot.require_fresh(self._now())
            except (TradingError, TypeError, ValueError, AttributeError):
                listener = self._expire_locked() if self._published is lease._published else self._notice_locked()
            else:
                return
        self._notify(listener)
        raise HotAccountUnavailable("账户热数据资格已失效，等待更新")
