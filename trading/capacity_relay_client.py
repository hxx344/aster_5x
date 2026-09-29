"""TLS-only public capacity relay, independent of exchange request budgets."""
from __future__ import annotations

from concurrent.futures import Future
from copy import deepcopy
from dataclasses import dataclass
import json
import logging
import math
import os
import re
import ssl
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID, uuid4

import httpx
from websockets.sync.client import connect as websocket_connect

from .models import TradingError


@dataclass(frozen=True)
class _Sample:
    started: float
    checked_at: float
    payload: object
    source: str
    received_at: float


class PublicCapacityRelayClient:
    """A shared stream cache with deduplicated, rate-limited HTTP cache reads.

    Construction performs no network I/O. Both transports verify certificates
    and hostnames using the same trust store. The relay token never enters URLs,
    exception messages, or public status. Tests can inject clocks/transports.
    """

    _MAX_AGE = {"oi": 8.0, "brackets": 300.0}
    _REFRESH_AGE = {"oi": 0.5, "brackets": 60.0}
    _FUTURE_ALLOWANCE = 1.0
    _CLOCK_TOLERANCE = 1.0
    _HTTP_INTERVAL = 0.2
    _OPEN_TIMEOUT = 3.0
    _CLOSE_TIMEOUT = 1.0
    _RECV_TIMEOUT = 0.25
    _RETRY_INITIAL = 0.5
    _RETRY_MAX = 30.0
    _MAX_MESSAGE = 2 * 1024 * 1024

    def __init__(self, url, token, ca_file=None, *, clock=None, monotonic=None,
                 http_client=None, transport=None, connect=None):
        try:
            parts = urlsplit(url)
            if (not isinstance(url, str) or parts.scheme != "https" or
                    not parts.hostname or parts.username is not None or
                    parts.password is not None or parts.query or parts.fragment or
                    any(character in url for character in ("?", "#", "\\")) or
                    any(character.isspace() or ord(character) < 32 for character in url)):
                raise ValueError
            # Evaluating port also rejects malformed or out-of-range ports.
            parts.port
        except (ValueError, TypeError, AttributeError):
            raise ValueError("容量中继地址必须为不含凭据、查询或片段的 HTTPS 地址") from None
        if (not isinstance(token, str) or not token or len(token) > 4096 or
                any(ord(character) < 33 or ord(character) > 126 for character in token)):
            raise ValueError("容量中继令牌格式无效")
        try:
            self._tls = ssl.create_default_context(cafile=ca_file)
        except (OSError, ValueError, ssl.SSLError):
            raise ValueError("容量中继 CA 文件无效") from None
        prefix = parts.path.rstrip("/")
        self._http_url = urlunsplit(("https", parts.netloc, prefix + "/v1/snapshot", "", ""))
        self._ws_url = urlunsplit(("wss", parts.netloc, prefix + "/v1/stream", "", ""))
        self._headers = {"Authorization": "Bearer " + token}
        self._http = http_client or httpx.Client(
            verify=self._tls, timeout=httpx.Timeout(3.0, connect=3.0),
            follow_redirects=False, trust_env=False, transport=transport)
        self._connect = connect or websocket_connect
        # websockets debug logging includes handshake headers. This dedicated
        # transport logger is deliberately disabled even if global DEBUG is on.
        self._ws_logger = logging.Logger(__name__ + ".transport")
        self._ws_logger.disabled = True
        self._clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._connected = False
        self._instance_id = str(uuid4())
        self._ws_connected_ticks = None
        self._ws_disconnected_ticks = self._monotonic()
        self._ws_oi_ticks = None
        self._ws_last_oi_sample_at = None
        self._ws_failure_count = 0
        self._connection_generation = 0
        self._epoch = None
        self._epoch_serial = 0
        self._retired_epochs = set()
        self._order = {}
        self._cache = {}
        self._inflight = {}
        self._last_attempt = {}
        self._update_listener = None
        self._last_error = None
        self._ws_connected_at = None
        self._ws_disconnected_at = None
        self._ws_last_message_at = None
        self._ws_last_sample_at = None
        self._ws_connection_attempts = 0
        self._ws_retry_at = None
        self._ws_last_error = None
        self._http_requests = 0
        self._http_failures = 0
        self._http_last_attempt_at = None
        self._http_last_success_at = None
        self._http_last_error = None

    @classmethod
    def from_env(cls, environ=None, **kwargs):
        values = os.environ if environ is None else environ
        url = values.get("ASTER_CAPACITY_RELAY_URL", "").strip()
        token = values.get("ASTER_CAPACITY_RELAY_TOKEN", "").strip()
        ca_file = values.get("ASTER_CAPACITY_RELAY_CA_FILE", "").strip() or None
        if not url and not token and not ca_file:
            return None
        if not url or not token:
            raise ValueError("容量中继需要同时配置 HTTPS 地址和令牌")
        return cls(url, token, ca_file=ca_file, **kwargs)

    def set_update_listener(self, listener):
        if listener is not None and not callable(listener):
            raise ValueError("容量中继更新回调无效")
        with self._lock:
            self._update_listener = listener

    def start(self):
        with self._lock:
            if self._stop.is_set() or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="capacity-relay", daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                raise

    def close(self):
        with self._lock:
            if self._stop.is_set():
                return
            self._stop.set()
            if self._connected:
                self._ws_disconnected_at = self._clock()
                self._ws_disconnected_ticks = self._monotonic()
            self._connected = False
            self._ws_connected_ticks = self._ws_oi_ticks = None
            self._ws_retry_at = None
            self._connection_generation += 1
            self._cache.clear()
            pending = tuple(self._inflight.values())
            self._inflight.clear()
            thread = self._thread
            for future in pending:
                if not future.done():
                    future.set_exception(TradingError("容量中继已关闭"))
        if thread is not None and thread is not threading.current_thread():
            thread.join(self._OPEN_TIMEOUT + self._CLOSE_TIMEOUT + self._RECV_TIMEOUT + 0.5)
        self._http.close()

    def status(self):
        with self._lock:
            wall, ticks = self._clock(), self._monotonic()
            samples = []
            for (kind, symbol), value in sorted(self._cache.items()):
                age = self._sample_age(value, wall, ticks)
                samples.append({"kind": kind, "symbol": symbol, "source": value.source,
                                "age_seconds": age if math.isfinite(age) else None,
                                "max_age_seconds": self._MAX_AGE[kind],
                                "received_at": value.received_at})
            return {"enabled": True, "instance_id": self._instance_id,
                    "running": self._thread is not None and self._thread.is_alive(),
                    "connected": self._connected, "closed": self._stop.is_set(),
                    "cached_samples": len(self._cache), "last_error": self._last_error,
                    "observed_at": wall,
                    "ws": {"connected_at": self._ws_connected_at,
                           "disconnected_at": self._ws_disconnected_at,
                           "last_message_at": self._ws_last_message_at,
                           "last_sample_at": self._ws_last_sample_at,
                           "last_oi_sample_at": self._ws_last_oi_sample_at,
                           "failure_count": self._ws_failure_count,
                           "connected_age_seconds": (max(0.0, ticks - self._ws_connected_ticks)
                               if self._connected and self._ws_connected_ticks is not None else None),
                           "disconnected_age_seconds": (max(0.0, ticks - self._ws_disconnected_ticks)
                               if not self._connected and self._ws_disconnected_ticks is not None else None),
                           "oi_idle_seconds": (max(0.0, ticks - (self._ws_oi_ticks
                               if self._ws_oi_ticks is not None else self._ws_connected_ticks))
                               if self._connected and self._ws_connected_ticks is not None else None),
                           "has_oi_sample": self._connected and self._ws_oi_ticks is not None,
                           "connection_attempts": self._ws_connection_attempts,
                           "retry_in_seconds": (max(0.0, self._ws_retry_at - ticks)
                                                if self._ws_retry_at is not None else None),
                           "last_error": self._ws_last_error},
                    "http": {"inflight": len(self._inflight), "requests": self._http_requests,
                             "failures": self._http_failures,
                             "last_attempt_at": self._http_last_attempt_at,
                             "last_success_at": self._http_last_success_at,
                             "last_error": self._http_last_error},
                    "samples": samples}

    @staticmethod
    def _key(kind, symbol):
        if kind not in ("oi", "brackets") or not isinstance(symbol, str) or not re.fullmatch(r"[A-Z0-9]{1,32}", symbol):
            raise TradingError("容量中继采样类型或交易对无效")
        return kind, symbol

    @staticmethod
    def _number(value):
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("Invalid relay time")
        return float(value)

    @staticmethod
    def _sample_age(value, wall, ticks):
        return max(ticks - value.started, wall - value.checked_at)

    def _read_locked(self, key, max_age):
        if self._stop.is_set():
            raise TradingError("容量中继已关闭")
        value = self._cache.get(key)
        if value is None:
            return None
        ticks, wall = self._monotonic(), self._clock()
        age = self._sample_age(value, wall, ticks)
        if not 0 <= age <= max_age:
            return None
        return value.started, value.checked_at, deepcopy(value.payload)

    def sample(self, kind, symbol, max_age):
        key = self._key(kind, symbol)
        try:
            max_age = min(self._number(max_age), self._MAX_AGE[kind])
        except (ValueError, TypeError, OverflowError):
            raise TradingError("容量中继有效期无效") from None
        with self._lock:
            cached = self._read_locked(key, max_age)
            preferred = self._read_locked(key, min(max_age, self._REFRESH_AGE[kind]))
            if preferred is not None:
                return preferred
            pending = self._inflight.get(key)
            owner = pending is None
            if owner:
                ticks = self._monotonic()
                if ticks - self._last_attempt.get(key, float("-inf")) < self._HTTP_INTERVAL:
                    if cached is not None:
                        return cached
                    raise TradingError("容量中继暂无足够新的数据")
                self._last_attempt[key] = ticks
                pending = self._inflight[key] = Future()
                epoch_serial = self._epoch_serial
                self._http_requests += 1
                self._http_last_attempt_at = self._clock()
        if owner:
            failure = None
            try:
                self._fetch(key, epoch_serial)
            except Exception:
                failure = TradingError("容量中继快照暂不可用")
                with self._lock:
                    if not self._stop.is_set():
                        self._last_error = "snapshot_unavailable"
                        self._http_last_error = "snapshot_unavailable"
                        self._http_failures += 1
            with self._lock:
                if self._inflight.get(key) is pending:
                    self._inflight.pop(key, None)
                if not pending.done():
                    if failure is None:
                        pending.set_result(None)
                    else:
                        pending.set_exception(failure)
        try:
            pending.result(timeout=self._OPEN_TIMEOUT + 1.0)
        except Exception:
            # A stream update received during the failed fallback may suffice.
            with self._lock:
                cached = self._read_locked(key, max_age)
                if cached is not None:
                    return cached
            raise TradingError("容量中继快照暂不可用") from None
        with self._lock:
            cached = self._read_locked(key, max_age)
            if cached is not None:
                return cached
        raise TradingError("容量中继暂无足够新的数据")

    def _fetch(self, key, epoch_serial):
        started = self._monotonic()
        started_wall = self._clock()
        with self._http.stream("GET", self._http_url, params={"kind": key[0], "symbol": key[1]},
                               headers=self._headers, follow_redirects=False) as response:
            if response.status_code != 200:
                raise ValueError("Invalid relay response")
            body = bytearray()
            for chunk in response.iter_bytes(chunk_size=64 * 1024):
                if len(chunk) > self._MAX_MESSAGE - len(body):
                    raise ValueError("Oversized relay response")
                body.extend(chunk)
        received, wall = self._monotonic(), self._clock()
        if (received < started or
                abs((wall - started_wall) - (received - started)) > self._CLOCK_TOLERANCE):
            raise ValueError("Invalid relay response")
        self._accept(json.loads(body), wall=wall, ticks=received,
                     network_age=received - started, expected_key=key, epoch_serial=epoch_serial,
                     source="http")

    def _decode_envelope(self, envelope, *, wall, network_age, expected_key):
        try:
            if not isinstance(envelope, dict) or type(envelope.get("version")) is not int or envelope["version"] != 1:
                return None
            key = self._key(envelope.get("kind"), envelope.get("symbol"))
            if expected_key is not None and key != expected_key:
                return None
            epoch = str(UUID(envelope["epoch"]))
            sequence = envelope["sequence"]
            if type(sequence) is not int or not 0 < sequence <= 2**63 - 1:
                return None
            sampled = self._number(envelope["sampled_at"])
            published = self._number(envelope["published_at"])
            source_age = self._number(envelope["age_ms"]) / 1000
            if (published < sampled or published - wall > self._FUTURE_ALLOWANCE or
                    sampled - wall > self._FUTURE_ALLOWANCE or
                    abs((published - sampled) - source_age) > self._CLOCK_TOLERANCE):
                return None
            payload = envelope["payload"]
            if not isinstance(payload, (dict, list)):
                return None
            age = max(source_age + network_age, source_age + abs(wall - published), wall - sampled)
            if not math.isfinite(age) or age > self._MAX_AGE[key[0]]:
                return None
        except (KeyError, TypeError, ValueError, OverflowError, AttributeError, TradingError):
            return None
        return key, epoch, sequence, age, payload

    def _accept(self, envelope, *, wall=None, ticks=None, network_age=0.0,
                expected_key=None, generation=None, epoch_serial=None, source="ws"):
        """Accept one original source sample; repeats cannot renew timestamps."""
        wall = self._clock() if wall is None else wall
        ticks = self._monotonic() if ticks is None else ticks
        decoded = self._decode_envelope(envelope, wall=wall, network_age=network_age,
                                        expected_key=expected_key)
        with self._lock:
            if self._stop.is_set() or (generation is not None and generation != self._connection_generation):
                return False
            # A valid HTTP response can contain a duplicate source sample. It
            # confirms transport availability without renewing any sample age.
            if source == "http":
                if decoded is None:
                    self._http_failures += 1
                    self._http_last_error = "snapshot_invalid"
                else:
                    self._http_last_success_at = wall
                    self._http_last_error = None
            if decoded is None:
                return False
            key, epoch, sequence, age, payload = decoded
            if epoch in self._retired_epochs:
                return False
            # In-flight HTTP from before an epoch transition cannot reverse it.
            if epoch_serial is not None and epoch_serial != self._epoch_serial and epoch != self._epoch:
                return False
            if epoch != self._epoch:
                if self._epoch is not None:
                    self._retired_epochs.add(self._epoch)
                self._epoch = epoch
                self._epoch_serial += 1
                self._cache.clear()
                self._order.clear()
            if sequence <= self._order.get(key, 0):
                return False
            self._order[key] = sequence
            self._cache[key] = _Sample(ticks - age, wall - age, deepcopy(payload), source, wall)
            if source == "ws":
                self._ws_last_sample_at = wall
                if key[0] == "oi":
                    self._ws_last_oi_sample_at = wall
                    if self._connected:
                        self._ws_oi_ticks = ticks
            self._last_error = None
            listener = self._update_listener
        if listener is not None and not self._stop.is_set():
            try:
                listener(key[1], key[0])
            except Exception:
                # Scheduler notifications must not interrupt the receiver.
                pass
        return True

    def _run(self):
        retry = self._RETRY_INITIAL
        while not self._stop.is_set():
            with self._lock:
                if self._stop.is_set():
                    return
                self._ws_connection_attempts += 1
                self._ws_retry_at = None
            connected_at = self._monotonic()
            generation = None
            try:
                with self._connect(self._ws_url, ssl=self._tls, additional_headers=self._headers,
                                   proxy=None, open_timeout=self._OPEN_TIMEOUT,
                                   close_timeout=self._CLOSE_TIMEOUT, max_size=self._MAX_MESSAGE,
                                   logger=self._ws_logger,
                                   ping_interval=20, ping_timeout=20) as connection:
                    with self._lock:
                        if self._stop.is_set():
                            return
                        self._connection_generation += 1
                        generation = self._connection_generation
                        self._connected = True
                        self._ws_connected_ticks = self._monotonic()
                        self._ws_disconnected_ticks = self._ws_oi_ticks = None
                        self._ws_connected_at = self._clock()
                        self._ws_last_error = None
                        self._last_error = None
                    while not self._stop.is_set():
                        try:
                            message = connection.recv(timeout=self._RECV_TIMEOUT)
                        except TimeoutError:
                            continue
                        with self._lock:
                            if not self._stop.is_set() and generation == self._connection_generation:
                                self._ws_last_message_at = self._clock()
                        try:
                            envelope = json.loads(message)
                        except (TypeError, ValueError, UnicodeError):
                            continue
                        self._accept(envelope, generation=generation)
            except Exception:
                with self._lock:
                    if not self._stop.is_set():
                        self._last_error = "stream_unavailable"
                        self._ws_last_error = "stream_unavailable"
            finally:
                with self._lock:
                    if generation is None or generation == self._connection_generation:
                        if not self._stop.is_set():
                            self._ws_failure_count += 1
                        if self._connected:
                            self._ws_disconnected_at = self._clock()
                            self._ws_disconnected_ticks = self._monotonic()
                        self._connected = False
                        self._ws_connected_ticks = self._ws_oi_ticks = None
            if self._monotonic() - connected_at >= 30:
                retry = self._RETRY_INITIAL
            with self._lock:
                if not self._stop.is_set():
                    self._ws_retry_at = self._monotonic() + retry
            if self._stop.wait(retry):
                break
            retry = min(self._RETRY_MAX, retry * 2)
