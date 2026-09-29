"""Credential-free public capacity collector with authenticated read-only delivery.

Importing this module performs no I/O. Only the lifespan collector contacts
Aster, using its two fixed public endpoints; HTTP and WS consumers read cache.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict, deque
from contextlib import asynccontextmanager, suppress
from copy import deepcopy
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import ssl
import time
import uuid

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
import httpx

import monitor


MAX_SYMBOLS = 16
MAX_CONNECTIONS = 64
MAX_REQUESTS_PER_MINUTE = 900
MIN_REQUEST_SPACING = 60 / MAX_REQUESTS_PER_MINUTE
MAX_PAYLOAD_BYTES = 2_000_000
MAX_AGE = {"oi": 3.0, "brackets": 300.0}
SYMBOL_PATTERN = re.compile(r"[A-Z0-9]{1,24}USD1")


class RelayConfigError(ValueError):
    pass


@dataclass(frozen=True)
class RelayConfig:
    token: str = field(repr=False)
    host: str = "0.0.0.0"
    port: int = 8766
    symbols: tuple[str, ...] = ("XAUUSD1",)
    interval: float = 0.2
    brackets_interval: float = 60.0
    cert_file: str = ""
    key_file: str = field(default="", repr=False)

    def __post_init__(self):
        if not isinstance(self.token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", self.token):
            raise RelayConfigError("ASTER_RELAY_TOKEN must contain 32-256 URL-safe ASCII characters")
        try:
            ipaddress.ip_address(self.host)
        except ValueError:
            raise RelayConfigError("ASTER_RELAY_HOST must be an IPv4 or IPv6 bind address") from None
        if type(self.port) is not int or not 1 <= self.port <= 65535:
            raise RelayConfigError("ASTER_RELAY_PORT must be between 1 and 65535")
        if (not isinstance(self.symbols, (tuple, list)) or not 1 <= len(self.symbols) <= MAX_SYMBOLS
                or any(not isinstance(symbol, str) or not SYMBOL_PATTERN.fullmatch(symbol) for symbol in self.symbols)
                or len(set(self.symbols)) != len(self.symbols)):
            raise RelayConfigError("ASTER_RELAY_SYMBOLS must contain 1-16 unique USD1 symbols")
        object.__setattr__(self, "symbols", tuple(self.symbols))
        for name, value, lower, upper in (("INTERVAL", self.interval, 0.2, 60),
                                          ("BRACKETS_INTERVAL", self.brackets_interval, 10, 300)):
            if type(value) not in (int, float) or not math.isfinite(value) or not lower <= value <= upper:
                raise RelayConfigError(f"ASTER_RELAY_{name} must be between {lower} and {upper} seconds")
        if bool(self.cert_file) != bool(self.key_file):
            raise RelayConfigError("ASTER_RELAY_CERT_FILE and ASTER_RELAY_KEY_FILE must be configured together")

    @property
    def effective_interval(self):
        # Reserve the global budget for the slower risk-table requests too.
        count = len(self.symbols)
        oi_rate = MAX_REQUESTS_PER_MINUTE / 60 - count / self.brackets_interval
        return max(float(self.interval), count / oi_rate)

    @classmethod
    def from_env(cls, environ=None):
        env = os.environ if environ is None else environ
        try:
            return cls(token=env.get("ASTER_RELAY_TOKEN", ""), host=env.get("ASTER_RELAY_HOST", "0.0.0.0"),
                port=int(env.get("ASTER_RELAY_PORT", "8766")),
                symbols=tuple(part.strip() for part in env.get("ASTER_RELAY_SYMBOLS", "XAUUSD1").split(",")),
                interval=float(env.get("ASTER_RELAY_INTERVAL", "0.2")),
                brackets_interval=float(env.get("ASTER_RELAY_BRACKETS_INTERVAL", "60")),
                cert_file=env.get("ASTER_RELAY_CERT_FILE", ""), key_file=env.get("ASTER_RELAY_KEY_FILE", ""))
        except RelayConfigError:
            raise
        except (TypeError, ValueError, OverflowError):
            raise RelayConfigError("Invalid numeric relay environment setting") from None

    def require_tls(self):
        if not self.cert_file or not self.key_file:
            raise RelayConfigError("Relay CLI requires ASTER_RELAY_CERT_FILE and ASTER_RELAY_KEY_FILE")
        if not Path(self.cert_file).is_file() or not Path(self.key_file).is_file():
            raise RelayConfigError("Relay TLS certificate or key file is missing")
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            context.load_cert_chain(self.cert_file, self.key_file)
        except (OSError, ssl.SSLError):
            raise RelayConfigError("Relay TLS certificate/key could not be loaded") from None


def validate_payload(kind, symbol, payload):
    """Check every relevant public number before distributing the raw object."""
    data = monitor.unwrap(payload)
    if kind == "oi":
        if data.get("symbol") != symbol:
            raise monitor.MonitorError("Public capacity symbol mismatch")
        mapping = data.get("leverageOiRemainingMap")
        if not isinstance(mapping, dict) or not 1 <= len(mapping) <= 125:
            raise monitor.MonitorError("Invalid leverage capacity map")
        for leverage, value in mapping.items():
            if (not isinstance(leverage, str) or not re.fullmatch(r"[1-9][0-9]{0,2}", leverage)
                    or not 1 <= int(leverage) <= 125):
                raise monitor.MonitorError("Invalid public leverage tier")
            monitor.number(value)
        return
    if kind != "brackets":
        raise monitor.MonitorError("Unsupported public capacity kind")
    rows = data.get("brackets")
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise monitor.MonitorError("Invalid public risk brackets")
    matches = [row for row in rows if row.get("symbol") == symbol]
    if len(matches) != 1:
        raise monitor.MonitorError("Missing or ambiguous public risk symbol")
    tiers = matches[0].get("riskBrackets")
    if not isinstance(tiers, list) or not 1 <= len(tiers) <= 125:
        raise monitor.MonitorError("Invalid public risk tiers")
    for tier in tiers:
        if not isinstance(tier, dict):
            raise monitor.MonitorError("Invalid public risk tier")
        low, high = (monitor.number(tier.get(key)) for key in ("minOpenPosLeverage", "maxOpenPosLeverage"))
        if not 1 <= low <= high <= 125 or low != int(low) or high != int(high):
            raise monitor.MonitorError("Invalid public risk leverage range")
        if not monitor.number(tier.get("bracketNotionalCap")):
            raise monitor.MonitorError("Invalid public risk cap")
        # This also rejects overlap with any other tier at its lower boundary.
        monitor.extract_bracket_cap(payload, symbol, int(low))


@dataclass(frozen=True)
class Sample:
    kind: str
    symbol: str
    sampled_at: float
    started_monotonic: float
    sequence: int
    payload: dict


class Subscriber:
    """At most one immutable sample per configured key; slow consumers coalesce."""
    def __init__(self):
        self.pending = OrderedDict()
        self.changed = asyncio.Event()
        self.closed = False

    def publish(self, sample):
        key = (sample.kind, sample.symbol)
        self.pending[key] = sample
        self.pending.move_to_end(key)
        self.changed.set()

    async def next(self):
        while not self.pending and not self.closed:
            await self.changed.wait()
            self.changed.clear()
        if self.closed:
            return None
        _, sample = self.pending.popitem(last=False)
        if not self.pending:
            self.changed.clear()
        return sample

    def close(self):
        self.closed = True
        self.pending.clear()
        self.changed.set()


class RelayCollector:
    def __init__(self, config, *, http=None, clock=None, monotonic=None):
        self.config = config
        self._http = http
        self._owns_http = http is None
        self.clock = clock or time.time
        self.monotonic = monotonic or time.monotonic
        self.epoch = str(uuid.uuid4())
        self.sequence = 0
        self.samples = {}
        self.errors = {}
        self.subscribers = set()
        self._inflight = set()
        self._requests = deque()
        self._next_request = 0.0
        self.backoff_until = 0.0
        self._task = None
        self._stopped = False
        now = self.monotonic()
        self._due = {(kind, symbol): now for kind in ("brackets", "oi") for symbol in config.symbols}

    async def start(self):
        if self._task is not None:
            return
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=3, follow_redirects=False, trust_env=False,
                limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
                headers={"Accept": "application/json", "Content-Type": "application/json",
                         "User-Agent": "AsterCapacityRelay/1.0", "Cache-Control": "no-cache"})
        self._stopped = False
        self._task = asyncio.create_task(self.run(), name="public-capacity-relay")

    async def stop(self):
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        for subscriber in tuple(self.subscribers):
            subscriber.close()
        self.subscribers.clear()
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    def _admission_delay(self, now):
        while self._requests and self._requests[0] <= now - 60:
            self._requests.popleft()
        until = max(self.backoff_until, self._next_request)
        if len(self._requests) >= MAX_REQUESTS_PER_MINUTE:
            until = max(until, self._requests[0] + 60)
        return max(0.0, until - now)

    def _retry_after(self, response):
        retry = {403: 60.0, 418: 86400.0, 429: 180.0}[response.status_code]
        value = response.headers.get("Retry-After", "")
        try:
            seconds = float(value)
        except (TypeError, ValueError, OverflowError):
            try:
                seconds = parsedate_to_datetime(value).timestamp() - self.clock()
            except (TypeError, ValueError, OverflowError, IndexError):
                seconds = 0
        if math.isfinite(seconds):
            retry = max(retry, seconds)
        return retry

    async def sample_once(self, kind, symbol):
        """One budgeted background read. Consumers never call this method."""
        key = (kind, symbol)
        if key not in self._due:
            raise ValueError("Unconfigured public capacity sample")
        now = self.monotonic()
        if key in self._inflight or self._admission_delay(now):
            return False
        if self._http is None:
            raise RuntimeError("Collector HTTP client is not initialized")
        self._inflight.add(key)
        self._requests.append(now)
        self._next_request = now + MIN_REQUEST_SPACING
        sampled_at = self.clock()
        try:
            method, path = ("GET", monitor.OI_PATH) if kind == "oi" else ("POST", monitor.BRACKETS_PATH)
            options = {"params": {"symbol": symbol}} if kind == "oi" else {"json": {"symbol": symbol}}
            # Streaming puts a hard bound on the response before JSON parsing.
            async with self._http.stream(method, monitor.BASE + path, **options) as response:
                if response.status_code in (403, 418, 429):
                    self.backoff_until = max(self.backoff_until, self.monotonic() + self._retry_after(response))
                if response.status_code != 200:
                    self.errors[key] = "upstream_http_" + str(response.status_code)
                    return False
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > MAX_PAYLOAD_BYTES:
                        raise ValueError("Oversized public payload")
            def reject_constant(_):
                raise ValueError("Invalid JSON number")
            payload = json.loads(body, parse_float=monitor.Decimal, parse_constant=reject_constant)
            validate_payload(kind, symbol, payload)
            # Preserve JSON decimal precision across transport. Public numeric
            # strings remain strings, and decimal JSON literals become exact
            # strings understood by the existing monitor.number parser.
            payload = json.loads(json.dumps(payload, default=str, allow_nan=False))
            self.sequence += 1
            sample = Sample(kind, symbol, sampled_at, now, self.sequence, payload)
            self.samples[key] = sample
            self.errors.pop(key, None)
            for subscriber in tuple(self.subscribers):
                subscriber.publish(sample)
            return True
        except (httpx.HTTPError, OSError, ValueError, TypeError, ArithmeticError, RecursionError, monitor.MonitorError):
            self.errors[key] = "upstream_unavailable_or_invalid"
            return False
        finally:
            self._inflight.discard(key)

    async def tick(self):
        now = self.monotonic()
        delay = self._admission_delay(now)
        if delay:
            return delay
        key = min(self._due, key=self._due.get)
        if self._due[key] > now:
            return self._due[key] - now
        interval = self.config.effective_interval if key[0] == "oi" else self.config.brackets_interval
        self._due[key] = now + interval
        await self.sample_once(*key)
        finished = self.monotonic()
        if self._due[key] <= finished:
            # A delayed request skips missed periods instead of catching up.
            self._due[key] = finished + interval
        return 0.0

    async def run(self):
        while not self._stopped:
            delay = await self.tick()
            # Bounded waits keep shutdown prompt even during a day-long ban.
            await asyncio.sleep(min(1.0, max(0.001, delay)))

    def envelope(self, sample):
        age = self.monotonic() - sample.started_monotonic
        if not 0 <= age <= MAX_AGE[sample.kind]:
            return None
        return {"version": 1, "epoch": self.epoch, "sequence": sample.sequence, "kind": sample.kind,
            "symbol": sample.symbol, "sampled_at": sample.sampled_at, "published_at": self.clock(),
            "age_ms": age * 1000, "payload": deepcopy(sample.payload)}

    def snapshot(self, kind, symbol):
        sample = self.samples.get((kind, symbol))
        return self.envelope(sample) if sample is not None else None

    def subscribe(self):
        if len(self.subscribers) >= MAX_CONNECTIONS:
            return None
        subscriber = Subscriber()
        self.subscribers.add(subscriber)
        for sample in sorted(self.samples.values(), key=lambda value: value.sequence):
            if self.envelope(sample) is not None:
                subscriber.publish(sample)
        return subscriber

    def unsubscribe(self, subscriber):
        self.subscribers.discard(subscriber)
        subscriber.close()

    def status(self):
        now = self.monotonic()
        rows = []
        for kind, symbol in self._due:
            sample = self.samples.get((kind, symbol))
            age = None if sample is None else (now - sample.started_monotonic) * 1000
            rows.append({"kind": kind, "symbol": symbol, "age_ms": age,
                "ready": age is not None and 0 <= age <= MAX_AGE[kind] * 1000,
                "last_error": self.errors.get((kind, symbol))})
        return {"configured_symbols": list(self.config.symbols), "samples": rows,
            "backoff_seconds": max(0.0, self.backoff_until - now), "connections": len(self.subscribers),
            "effective_interval_seconds": self.config.effective_interval,
            "brackets_interval_seconds": self.config.brackets_interval,
            "collector_running": self._task is not None and not self._task.done()}


def create_app(config=None, *, collector=None, http=None, clock=None, monotonic=None, start_collector=True):
    config = config or (collector.config if collector is not None else RelayConfig.from_env())
    collector = collector or RelayCollector(config, http=http, clock=clock, monotonic=monotonic)

    @asynccontextmanager
    async def lifespan(app):
        if start_collector:
            await collector.start()
        try:
            yield
        finally:
            await collector.stop()

    app = FastAPI(title="Aster Public Capacity Relay", docs_url=None, redoc_url=None,
                  openapi_url=None, lifespan=lifespan)
    app.state.collector = collector

    def authorized(connection):
        if any(key.lower() in {"token", "access_token", "authorization"} for key in connection.query_params):
            return False
        headers = connection.headers.getlist("authorization")
        if len(headers) != 1 or not headers[0].startswith("Bearer ") or len(headers[0]) > 512:
            return False
        return hmac.compare_digest(headers[0][7:].encode("utf-8"), config.token.encode("ascii"))

    def authenticate(request):
        if not authorized(request):
            raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/health")
    async def health(request: Request):
        authenticate(request)
        return {"status": "ok"}

    @app.get("/v1/status")
    async def status(request: Request):
        authenticate(request)
        return JSONResponse(collector.status(), headers={"Cache-Control": "no-store"})

    @app.get("/v1/snapshot")
    async def snapshot(request: Request, symbol: str = "", kind: str = ""):
        authenticate(request)
        if symbol not in config.symbols:
            raise HTTPException(400, "Symbol is not configured on this relay")
        if kind not in MAX_AGE:
            raise HTTPException(400, "Unsupported sample kind")
        value = collector.snapshot(kind, symbol)
        if value is None:
            raise HTTPException(503, "Sample unavailable or stale", headers={"Retry-After": "1"})
        return JSONResponse(value, headers={"Cache-Control": "no-store"})

    @app.websocket("/v1/stream")
    async def stream(websocket: WebSocket):
        if not authorized(websocket) or websocket.query_params:
            await websocket.close(code=1008)
            return
        subscriber = collector.subscribe()
        if subscriber is None:
            await websocket.close(code=1013)
            return
        async def send():
            while True:
                sample = await subscriber.next()
                if sample is None:
                    return
                value = collector.envelope(sample)
                if value is not None:
                    await asyncio.wait_for(websocket.send_json(value), timeout=5)

        async def receive():
            # V1 is server-push only. Read solely to notice disconnects; any
            # application message is unsupported and closes the connection.
            message = await websocket.receive()
            if message["type"] != "websocket.disconnect":
                await websocket.close(code=1008)

        tasks = []
        try:
            await websocket.accept()
            tasks = [asyncio.create_task(send()), asyncio.create_task(receive())]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (WebSocketDisconnect, TimeoutError, RuntimeError):
            with suppress(WebSocketDisconnect, RuntimeError):
                await websocket.close(code=1013)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            collector.unsubscribe(subscriber)

    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description="Authenticated public-only Aster capacity relay")
    parser.add_argument("--check-config", action="store_true", help="Validate local configuration and TLS files without network")
    args = parser.parse_args(argv)
    try:
        config = RelayConfig.from_env()
        config.require_tls()
    except RelayConfigError as exc:
        parser.error(str(exc))
    if args.check_config:
        print(json.dumps({"status": "ok", "configured_symbols": list(config.symbols),
                          "effective_interval_seconds": config.effective_interval}))
        return
    import uvicorn
    uvicorn.run(create_app(config), host=config.host, port=config.port, workers=1, access_log=False,
        ssl_certfile=config.cert_file, ssl_keyfile=config.key_file, ssl_version=ssl.PROTOCOL_TLS_SERVER,
        ws="websockets", ws_max_size=1024, ws_max_queue=4, timeout_keep_alive=5)


if __name__ == "__main__":
    main()
