"""Private account events revoke account leases; they never synthesize balances."""
from __future__ import annotations

import json
import logging
import re
import threading
import time

from websockets.sync.client import connect as websocket_connect


_LOG = logging.getLogger(__name__)
_LISTEN_PATH = "/fapi/v3/listenKey"
_BASE_URL = "wss://fstream.asterdex.com/ws/"


class _Reconnect(Exception):
    pass


class PrivateAccountStream:
    """Manage one signed listen key entirely on a background daemon.

    Aster has no documented account sequence/replay bridge. Every valid event
    is therefore an invalidation signal; only a later REST refresh is eligible.
    """

    _OPEN_TIMEOUT = 3
    _CLOSE_TIMEOUT = 1
    _RECV_TIMEOUT = 0.25
    _REST_TIMEOUT = 8
    _RETRY_INITIAL = 0.5
    _RETRY_MAX = 30
    _STABLE_SECONDS = 30
    _KEEPALIVE_SECONDS = 30 * 60
    _RECONNECT_SECONDS = 23 * 60 * 60 + 50 * 60

    def __init__(self, api, on_state, on_event, *, connect=None, clock=None, monotonic=None):
        if not callable(on_state) or not callable(on_event):
            raise ValueError("Invalid private account stream listener")
        self._api = api
        self._on_state = on_state
        self._on_event = on_event
        self._connect = connect or websocket_connect
        self._clock = clock
        self._monotonic = monotonic
        self._lock = threading.Lock()
        # Serializes state callbacks so a late connect cannot overtake close.
        self._state_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._connected = False

    def _ticks(self):
        return time.monotonic() if self._monotonic is None else self._monotonic()

    def _set_connected(self, connected):
        with self._state_lock:
            connected = bool(connected) and not self._stop.is_set()
            self._connected = connected
            try:
                self._on_state(connected)
            except Exception:
                self._connected = False
                # No callback error or private response is included in logs.
                if connected:
                    try:
                        self._on_state(False)
                    except Exception:
                        pass
                raise _Reconnect("Account state listener failed") from None
            return connected

    def start(self):
        with self._lock:
            if self._stop.is_set() or self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="private-account", daemon=True)
            try:
                self._thread.start()
            except Exception:
                self._thread = None
                raise

    def close(self):
        self._stop.set()
        try:
            self._set_connected(False)
        except _Reconnect:
            pass
        with self._lock:
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(self._REST_TIMEOUT + self._OPEN_TIMEOUT + self._CLOSE_TIMEOUT + self._RECV_TIMEOUT + 0.5)

    def _handle_message(self, message):
        try:
            event = json.loads(message)
            if not isinstance(event, dict):
                raise ValueError
            kind = event.get("e")
            if not isinstance(kind, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,79}", kind):
                raise ValueError
        except (TypeError, ValueError, UnicodeError):
            self._set_connected(False)
            raise _Reconnect("Invalid private account event") from None
        if kind == "listenKeyExpired":
            self._set_connected(False)
            raise _Reconnect("Private account listen key expired")
        with self._state_lock:
            if self._stop.is_set() or not self._connected:
                return
            try:
                self._on_event(kind)
            except Exception:
                self._set_connected(False)
                raise _Reconnect("Account event listener failed") from None

    def _create_key(self):
        data = self._api.call("POST", _LISTEN_PATH, signed=True, weight=1)
        key = data.get("listenKey") if isinstance(data, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", key):
            raise _Reconnect("Invalid private account listen key")
        return key

    def _keepalive(self):
        result = self._api.call("PUT", _LISTEN_PATH, signed=True, weight=1)
        if not isinstance(result, dict) or result:
            raise _Reconnect("Invalid private account renewal response")

    def _run(self):
        retry = self._RETRY_INITIAL
        while not self._stop.is_set():
            connection = None
            connected_at = None
            try:
                key = self._create_key()
                if self._stop.is_set():
                    return
                renewed_at = self._ticks()
                connection = self._connect(_BASE_URL + key,
                    open_timeout=self._OPEN_TIMEOUT, close_timeout=self._CLOSE_TIMEOUT,
                    ping_interval=20, ping_timeout=20, max_size=65536,
                    max_queue=16, compression=None)
                if not self._set_connected(True):
                    return
                connected_at = self._ticks()
                while not self._stop.is_set():
                    now = self._ticks()
                    if now - connected_at >= self._RECONNECT_SECONDS:
                        raise _Reconnect("Scheduled private account reconnect")
                    if now - renewed_at >= self._KEEPALIVE_SECONDS:
                        self._keepalive()
                        renewed_at = now
                        if self._stop.is_set():
                            return
                    try:
                        message = connection.recv(timeout=self._RECV_TIMEOUT)
                    except TimeoutError:
                        continue
                    self._handle_message(message)
            except Exception as exc:
                _LOG.debug("Private account stream disconnected (%s)", type(exc).__name__)
            finally:
                try:
                    self._set_connected(False)
                except _Reconnect:
                    pass
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
