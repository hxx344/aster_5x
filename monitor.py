"""Public Aster leverage-capacity monitor. Python 3.9+, standard library only."""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import logging
import math
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal
import socket
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parent
BASE = "https://www.asterdex.com"
OI_PATH = "/bapi/futures/v1/public/future/common/symbol/leverageoi/remaining"
BRACKETS_PATH = "/bapi/futures/v1/friendly/future/common/brackets"
LOG = logging.getLogger("aster")
SUPPORTED_SYMBOLS = ("XAUUSD1", "SPCXUSD1", "CLUSD1")
SUPPORTED_LEVERAGES = (5, 10, 20)


class MonitorError(Exception):
    def __init__(self, message, retry_after=0):
        super().__init__(message)
        self.retry_after = retry_after


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def number(value):
    if isinstance(value, bool) or value is None:
        raise MonitorError("Missing or invalid amount")
    text = str(value)
    if len(text) > 128:
        raise MonitorError("Amount exceeds supported length")
    try:
        result = Decimal(text)
    except InvalidOperation:
        raise MonitorError("Invalid amount") from None
    if not result.is_finite() or result < 0:
        raise MonitorError("Non-finite or negative amount")
    if abs(result.as_tuple().exponent) > 100:
        raise MonitorError("Amount precision or exponent exceeds supported range")
    return result


def request_json(url, body=None, timeout=10):
    payload = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(url, data=payload, headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "User-Agent": "AsterCapacityMonitor/1.0", "Cache-Control": "no-cache",
    })
    try:
        with build_opener(NoRedirect).open(request, timeout=timeout) as response:
            return json.loads(response.read(2_000_000), parse_float=Decimal)
    except HTTPError as exc:
        retry = 0
        if exc.code in (418, 429):
            retry = 180 if exc.code == 429 else 86400
            try:
                requested = float(exc.headers.get("Retry-After", 0))
                if math.isfinite(requested):
                    retry = max(retry, requested)
            except (TypeError, ValueError):
                pass
        # Do not expose webhook URLs or remote error bodies in logs.
        raise MonitorError(f"HTTP {exc.code}", retry) from None
    except (URLError, TimeoutError, OSError, ValueError):
        raise MonitorError("Network error or invalid JSON response") from None


def unwrap(payload):
    if not isinstance(payload, dict) or payload.get("success") is not True or str(payload.get("code")) != "000000":
        raise MonitorError("Aster rejected request or changed response format")
    if not isinstance(payload.get("data"), dict):
        raise MonitorError("Aster response missing data")
    return payload["data"]


def extract_capacity(oi_payload, brackets_payload, symbol, leverage):
    oi = unwrap(oi_payload)
    if oi.get("symbol") != symbol:
        raise MonitorError("Aster returned a different symbol")
    mapping = oi.get("leverageOiRemainingMap")
    # This monitor deliberately requires the exact requested tier.
    if not isinstance(mapping, dict) or str(leverage) not in mapping:
        raise MonitorError("Requested leverage tier missing")
    remaining = number(mapping[str(leverage)])
    brackets = unwrap(brackets_payload).get("brackets")
    if not isinstance(brackets, list):
        raise MonitorError("Risk brackets missing")
    matches = []
    for row in brackets:
        if not isinstance(row, dict):
            raise MonitorError("Invalid bracket row")
        if row.get("symbol") != symbol:
            continue
        tiers = row.get("riskBrackets")
        if not isinstance(tiers, list):
            raise MonitorError("Risk bracket tiers missing")
        for tier in tiers:
            if not isinstance(tier, dict):
                raise MonitorError("Invalid risk bracket tier")
            if number(tier.get("minOpenPosLeverage")) <= leverage <= number(tier.get("maxOpenPosLeverage")):
                matches.append(tier)
    if len(matches) != 1:
        raise MonitorError("Requested leverage bracket missing or ambiguous")
    cap = number(matches[0].get("bracketNotionalCap"))
    return min(remaining, cap), remaining, cap


def load_config():
    try:
        config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
        local = Path(os.environ.get("ASTER_CONFIG_FILE", ROOT / "config.local.json"))
        if local.exists():
            overrides = json.loads(local.read_text(encoding="utf-8-sig"))
            if not isinstance(overrides, dict):
                raise MonitorError("Configuration must be a JSON object")
            config.update(overrides)
    except (OSError, ValueError):
        raise MonitorError("Cannot read configuration or invalid JSON") from None
    return validate_config(config)


def validate_config(config):
    config = config.copy()
    # Old installations could persist the original single-symbol default.
    # Keep their notification settings while adopting the expanded default list.
    legacy_symbol = config.pop("symbol", None)
    legacy_leverage = config.pop("leverage", None)
    if legacy_symbol not in (None, "XAUUSD1") or legacy_leverage not in (None, 5):
        raise MonitorError("Unsupported legacy symbol or leverage")
    leverages = config.get("leverages", list(SUPPORTED_LEVERAGES))
    # Drop the retired tier from old installations without discarding their settings.
    if (not isinstance(leverages, list) or not leverages
            or any(type(v) is not int or (v not in SUPPORTED_LEVERAGES and v != 4) for v in leverages)
            or len(set(leverages)) != len(leverages)):
        raise MonitorError("leverages must contain unique supported tiers: 5, 10, 20")
    config["leverages"] = [v for v in leverages if v in SUPPORTED_LEVERAGES] or list(SUPPORTED_LEVERAGES)
    symbols = config.get("symbols", list(SUPPORTED_SYMBOLS))
    if not isinstance(symbols, list) or not symbols or any(s not in SUPPORTED_SYMBOLS for s in symbols) or len(set(symbols)) != len(symbols):
        raise MonitorError("symbols must contain unique supported symbols: XAUUSD1, SPCXUSD1, CLUSD1")
    config["symbols"] = symbols.copy()
    config["threshold"] = number(config["threshold"])
    for key, minimum in [("poll_seconds", 5), ("timeout_seconds", 1), ("cooldown_seconds", 0)]:
        value = number(config[key])
        if value < minimum or value > 86400:
            raise MonitorError(f"Invalid {key}")
        config[key] = float(value)
    if not isinstance(config.get("feishu_enabled"), bool):
        raise MonitorError("feishu_enabled must be true or false")
    config["webhook"] = os.environ.get("FEISHU_WEBHOOK_URL", config.get("feishu_webhook", ""))
    config["secret"] = os.environ.get("FEISHU_SIGN_SECRET", config.get("feishu_sign_secret", ""))
    if config["feishu_enabled"]:
        u = urlparse(config["webhook"])
        if u.scheme != "https" or u.netloc != "open.feishu.cn" or not u.path.startswith("/open-apis/bot/v2/hook/") or not u.path.rsplit("/", 1)[-1] or u.query or u.fragment:
            raise MonitorError("A valid Feishu webhook is required when enabled")
    return config


def feishu_payload(message, secret, timestamp):
    payload = {"msg_type": "text", "content": {"text": message}}
    if secret:
        key = f"{timestamp}\n{secret}".encode()
        payload.update(timestamp=str(timestamp), sign=base64.b64encode(
            hmac.new(key, b"", hashlib.sha256).digest()).decode())
    return payload


def send_feishu(config, message):
    response = request_json(config["webhook"], feishu_payload(message, config["secret"], int(time.time())), config["timeout_seconds"])
    if not isinstance(response, dict) or response.get("code", response.get("StatusCode")) != 0:
        raise MonitorError("Feishu did not acknowledge delivery")


class AlertGate:
    def __init__(self, state=None):
        self.state = state or {"notified": False, "last_alert": 0}

    def observe(self, value, threshold, now, cooldown, deliver):
        if value <= threshold:
            self.state["notified"] = False
            return False
        if self.state.get("notified") or now - self.state.get("last_alert", 0) < cooldown:
            return False
        deliver()  # A failed delivery keeps the alert eligible for retry.
        self.state.update(notified=True, last_alert=now)
        return True


def atomic_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def sample(config):
    query = urlencode({"symbol": config["symbol"]})
    brackets = request_json(BASE + BRACKETS_PATH, {"symbol": config["symbol"]}, config["timeout_seconds"])
    oi = request_json(BASE + OI_PATH + "?" + query, timeout=config["timeout_seconds"])
    results = {}
    checked_at = now_iso()
    for leverage in config["leverages"]:
        try:
            value, remaining, cap = extract_capacity(oi, brackets, config["symbol"], leverage)
            results[leverage] = {"value": str(value), "global_remaining": str(remaining), "bracket_cap": str(cap), "checked_at": checked_at}
        except (MonitorError, KeyError, TypeError, ValueError) as exc:
            results[leverage] = exc
    return results


def runtime_dir():
    return Path(os.environ.get("ASTER_RUNTIME_DIR", ROOT / "runtime"))


def alert_identity(config, symbol):
    # Preserve the original hash so existing XAUUSD1 alerts survive upgrades.
    return hashlib.sha256(f"{symbol}|{config['leverage']}|{config['threshold']}|{config['feishu_enabled']}|{config['webhook']}".encode()).hexdigest()


def restore_gate(saved, config, symbol):
    record = saved.get("markets", {}).get(market_key(symbol, config["leverage"]), {})
    if not record and config["leverage"] == 5:
        record = saved.get("markets", {}).get(symbol, {})
    if not record and symbol == "XAUUSD1" and config["leverage"] == 5:
        record = saved  # Legacy single-market alerts.json.
    if isinstance(record, dict) and record.get("identity") == alert_identity(config, symbol):
        gate = record.get("gate")
        if isinstance(gate, dict) and isinstance(gate.get("notified"), bool):
            try:
                number(gate.get("last_alert"))
                return AlertGate(gate)
            except MonitorError:
                pass
    return AlertGate()


def market_key(symbol, leverage):
    return f"{symbol}:{leverage}"


class MarketMonitor:
    """Independent alert state for one symbol and leverage combination."""
    def __init__(self, config, symbol, saved, shutdown):
        self.config = {**config, "symbol": symbol}
        self.symbol = symbol
        self.shutdown = shutdown
        self.gate = restore_gate(saved, config, symbol)
        self.failures = 0
        self.status = {"symbol": symbol, "leverage": config["leverage"], "status": "starting"}

    def deliver(self, value, result):
        config = self.config
        message = (f"Aster 开仓额度提醒\n{self.symbol} · {config['leverage']}x\n"
                   f"公开剩余可开额度：{value:,.2f} USD1\n"
                   f"触发条件：> {config['threshold']:,.2f} USD1\n"
                   "未扣除个人持仓和挂单占用，请以账户页面为准。\n"
                   f"检查时间：{result['checked_at']}\n"
                   f"https://www.asterdex.com/zh-CN/trade/pro/futures/{self.symbol}")
        if config["feishu_enabled"]:
            send_feishu(config, message)
        LOG.warning("THRESHOLD_ALERT symbol=%s leverage=%sx value=%s USD1 delivery=%s", self.symbol,
                    config["leverage"], value, "feishu" if config["feishu_enabled"] else "local_only")

    def check(self, result):
        config = self.config
        status = self.status.copy()
        delay = config["poll_seconds"]
        retry_after = 0
        try:
            if isinstance(result, Exception):
                raise result
            if self.shutdown.is_set():
                return None
            value = number(result["value"])
            status.update(result, status="ok", above_threshold=value > config["threshold"], error=None)
            self.gate.observe(value, config["threshold"], time.time(), config["cooldown_seconds"],
                              lambda: self.deliver(value, result))
            status.pop("failed_at", None)
            status.pop("retry_seconds", None)
            LOG.info("%s %sx capacity=%s USD1 above_threshold=%s", self.symbol,
                     config["leverage"], value, status["above_threshold"])
            self.failures = 0
        except (MonitorError, KeyError, TypeError, ValueError) as exc:
            self.failures += 1
            failure_retry = getattr(exc, "retry_after", 0)
            # Only a failed market sample can throttle all Aster queries.
            # Feishu backoff still delays this tier's delivery retry.
            retry_after = failure_retry if exc is result else 0
            delay = max(min(300, config["poll_seconds"] * 2 ** min(self.failures, 6)), failure_retry)
            status.update(status="error", error=str(exc) if isinstance(exc, MonitorError) else "Unexpected response format",
                          failed_at=now_iso(), retry_seconds=delay, above_threshold=None)
            LOG.error("%s %sx check failed: %s; retry in %ss", self.symbol, config["leverage"], status["error"], delay)
        self.status = status
        record = {"identity": alert_identity(config, self.symbol), "gate": self.gate.state.copy()}
        return status.copy(), record, delay, retry_after


class SymbolMonitor:
    """Fetch one shared snapshot per symbol; evaluate each tier independently."""
    def __init__(self, config, symbol, saved, shutdown):
        self.config = {**config, "symbol": symbol}
        self.trackers = {market_key(symbol, leverage): MarketMonitor(
            {**config, "leverage": leverage}, symbol, saved, shutdown)
            for leverage in config["leverages"]}
        self.next_due = dict.fromkeys(self.trackers, 0.0)

    def check(self):
        try:
            readings = sample(self.config)
        except (MonitorError, KeyError, TypeError, ValueError) as exc:
            readings = dict.fromkeys(self.config["leverages"], exc)
        markets, records, delays, retries = {}, {}, [], []
        for key, tracker in self.trackers.items():
            now = time.monotonic()
            if now < self.next_due[key]:
                delays.append(self.next_due[key] - now)
                continue
            result = tracker.check(readings.get(tracker.config["leverage"], MonitorError("Requested leverage tier missing")))
            if result is not None:
                markets[key], records[key], delay, retry = result
                self.next_due[key] = now + delay if markets[key]["status"] == "error" else 0
                delays.append(delay)
                retries.append(retry)
        if not markets:
            return None
        return markets, records, min(delays), max(retries)


def summarize(status):
    markets = list(status["markets"].values())
    states = [row["status"] for row in markets]
    status["status"] = ("ok" if all(s == "ok" for s in states) else
                        "starting" if all(s == "starting" for s in states) else
                        "error" if all(s == "error" for s in states) else "partial")
    times = [row.get("checked_at") for row in markets]
    if all(times):
        status["checked_at"] = min(times)
    status["heartbeat_at"] = now_iso()


def run(config, once=False, shutdown=None):
    shutdown = shutdown or threading.Event()
    runtime = runtime_dir()
    runtime.mkdir(exist_ok=True)
    # The OS releases this lock even after a crash; no stale PID can block restart.
    lock = socket.socket()
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        lock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    try:
        lock.bind(("127.0.0.1", 19551))
    except OSError:
        raise MonitorError("Another monitor is running, or local lock port 19551 is occupied") from None
    state_path = runtime / "alerts.json"
    saved = {}
    if state_path.exists():
        try:
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or not isinstance(saved.get("markets", {}), dict):
                raise ValueError("Invalid alert state")
        except (ValueError, OSError):
            LOG.warning("Alert state unreadable; starting a new alert episode")
            saved = {}
    trackers = {symbol: SymbolMonitor(config, symbol, saved, shutdown) for symbol in config["symbols"]}
    tiers = {key: tier for tracker in trackers.values() for key, tier in tracker.trackers.items()}
    records = saved.get("markets", {}).copy()
    records.update({key: {"identity": alert_identity(tier.config, tier.symbol), "gate": tier.gate.state.copy()}
                    for key, tier in tiers.items()})
    status = {"pid": os.getpid(), "symbols": config["symbols"], "leverages": config["leverages"],
              "threshold": str(config["threshold"]), "poll_seconds": config["poll_seconds"],
              "feishu_enabled": config["feishu_enabled"], "scope": "public_capacity_without_account_positions",
              "started_at": now_iso(), "status": "starting",
              "markets": {key: tier.status.copy() for key, tier in tiers.items()}}
    stop_file = runtime / "stop"
    next_due = dict.fromkeys(trackers, 0.0)
    pending = {}
    completed = set()
    rate_limit_until = 0.0
    exit_code = 0
    try:
        atomic_json(runtime / "status.json", status)
        with ThreadPoolExecutor(max_workers=len(trackers)) as pool:
            try:
                while not stop_file.exists() and not shutdown.is_set():
                    changed = False
                    # Consume results before scheduling, so rate limits apply to all new work.
                    for symbol, (future, started) in list(pending.items()):
                        if not future.done():
                            continue
                        result = future.result()
                        del pending[symbol]
                        if result is None:
                            continue
                        market, record, delay, retry_after = result
                        status["markets"].update(market)
                        records.update(record)
                        # Start retry delays after completion, including slow network calls.
                        next_due[symbol] = time.monotonic() + delay
                        if retry_after:
                            rate_limit_until = max(rate_limit_until, time.monotonic() + retry_after)
                        completed.add(symbol)
                        changed = True
                    if changed:
                        atomic_json(state_path, {"version": 3, "markets": records})
                        summarize(status)
                        atomic_json(runtime / "status.json", status)
                    if once and len(completed) == len(trackers):
                        exit_code = 0 if status["status"] == "ok" else 1
                        print(json.dumps(status, ensure_ascii=False))
                        break
                    now = time.monotonic()
                    for symbol, tracker in trackers.items():
                        if symbol not in pending and not (once and symbol in completed) and now >= max(next_due[symbol], rate_limit_until):
                            pending[symbol] = (pool.submit(tracker.check), now)
                    shutdown.wait(0.1)
            finally:
                shutdown.set()
    finally:
        # Preserve acknowledgements completed during graceful shutdown as well.
        for symbol, (future, _) in pending.items():
            if future.done() and not future.cancelled() and future.exception() is None:
                result = future.result()
                if result is not None:
                    status["markets"].update(result[0])
                    records.update(result[1])
        atomic_json(state_path, {"version": 3, "markets": records})
        summarize(status)
        status.update(status="completed" if once else "stopped", stopped_at=now_iso())
        atomic_json(runtime / "status.json", status)
        lock.close()
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Check once; enabled Feishu delivery still applies")
    parser.add_argument("--stop", action="store_true", help="Request graceful shutdown")
    args = parser.parse_args()
    runtime = runtime_dir()
    runtime.mkdir(exist_ok=True)
    if args.stop:
        (runtime / "stop").touch()
        return 0
    handler = RotatingFileHandler(runtime / "monitor.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handlers = [handler]
    if os.environ.get("ASTER_JOURNAL") == "1":
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)
    shutdown = threading.Event()
    for event in (signal.SIGTERM, signal.SIGINT):
        signal.signal(event, lambda *_: shutdown.set())
    try:
        return run(load_config(), args.once, shutdown)
    except MonitorError as exc:
        LOG.error("Startup failed: %s", exc)
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
