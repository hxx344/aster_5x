"""Public Aster leverage-capacity monitor. Python 3.11+, standard library only."""
import argparse
import base64
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import socket
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

ROOT = Path(__file__).resolve().parent
BASE = "https://www.asterdex.com"
OI_PATH = "/bapi/futures/v1/public/future/common/symbol/leverageoi/remaining"
BRACKETS_PATH = "/bapi/futures/v1/friendly/future/common/brackets"
LOG = logging.getLogger("aster")


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
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise MonitorError("Invalid amount") from None
    if not result.is_finite() or result < 0:
        raise MonitorError("Non-finite or negative amount")
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
                retry = max(retry, float(exc.headers.get("Retry-After", 0)))
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
    config = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    local = ROOT / "config.local.json"
    if local.exists():
        config.update(json.loads(local.read_text(encoding="utf-8-sig")))
    if config.get("symbol") != "XAUUSD1" or config.get("leverage") != 5:
        raise MonitorError("This monitor is configured only for XAUUSD1 at 5x")
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
    value, remaining, cap = extract_capacity(oi, brackets, config["symbol"], config["leverage"])
    return {"value": str(value), "global_remaining": str(remaining), "bracket_cap": str(cap), "checked_at": now_iso()}


def run(config, once=False):
    runtime = ROOT / "runtime"
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
    identity = hashlib.sha256(f"{config['symbol']}|{config['leverage']}|{config['threshold']}|{config['feishu_enabled']}|{config['webhook']}".encode()).hexdigest()
    state = {}
    if state_path.exists():
        try:
            saved = json.loads(state_path.read_text(encoding="utf-8"))
            if saved.get("identity") == identity:
                state = saved.get("gate", {})
        except (ValueError, OSError):
            LOG.warning("Alert state unreadable; starting a new alert episode")
    gate = AlertGate(state)
    status = {"pid": os.getpid(), "symbol": config["symbol"], "leverage": 5,
              "threshold": str(config["threshold"]), "poll_seconds": config["poll_seconds"],
              "feishu_enabled": config["feishu_enabled"], "scope": "public_capacity_without_account_positions",
              "started_at": now_iso(), "status": "starting"}
    stop_file = runtime / "stop"
    failures = 0
    exit_code = 0
    try:
        while not stop_file.exists():
            started = time.monotonic()
            delay = config["poll_seconds"]
            try:
                result = sample(config)
                value = number(result["value"])
                status.update(result, status="ok", above_threshold=value > config["threshold"], error=None)
                def deliver():
                    message = (f"Aster 开仓额度提醒\nXAUUSD1 · 5x\n公开剩余可开额度：{value:,.2f} USD1\n"
                               f"触发条件：> {config['threshold']:,.2f} USD1\n"
                               "未扣除个人持仓和挂单占用，请以账户页面为准。\n"
                               f"检查时间：{result['checked_at']}\n"
                               "https://www.asterdex.com/zh-CN/trade/pro/futures/XAUUSD1")
                    if config["feishu_enabled"]:
                        send_feishu(config, message)
                    LOG.warning("THRESHOLD_ALERT value=%s USD1 delivery=%s", value,
                                "feishu" if config["feishu_enabled"] else "local_only")
                gate.observe(value, config["threshold"], time.time(), config["cooldown_seconds"], deliver)
                atomic_json(state_path, {"identity": identity, "gate": gate.state})
                LOG.info("XAUUSD1 5x capacity=%s USD1 above_threshold=%s", value, status["above_threshold"])
                failures = 0
                exit_code = 0
            except (MonitorError, KeyError, TypeError, ValueError) as exc:
                failures += 1
                delay = max(min(300, config["poll_seconds"] * 2 ** min(failures, 6)), getattr(exc, "retry_after", 0))
                status.update(status="error", error=str(exc) if isinstance(exc, MonitorError) else "Unexpected response format", failed_at=now_iso(), retry_seconds=delay)
                LOG.error("Check failed: %s; retry in %ss", status["error"], delay)
                exit_code = 1
            status["heartbeat_at"] = now_iso()
            atomic_json(runtime / "status.json", status)
            if once:
                print(json.dumps(status, ensure_ascii=False))
                break
            deadline = started + delay
            while not stop_file.exists() and time.monotonic() < deadline:
                time.sleep(min(0.5, max(0, deadline - time.monotonic())))
    finally:
        status.update(status="completed" if once else "stopped", stopped_at=now_iso())
        atomic_json(runtime / "status.json", status)
        lock.close()
    return exit_code


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Check once; enabled Feishu delivery still applies")
    parser.add_argument("--stop", action="store_true", help="Request graceful shutdown")
    args = parser.parse_args()
    runtime = ROOT / "runtime"
    runtime.mkdir(exist_ok=True)
    if args.stop:
        (runtime / "stop").touch()
        return 0
    handler = RotatingFileHandler(runtime / "monitor.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=[handler])
    try:
        return run(load_config(), args.once)
    except MonitorError as exc:
        LOG.error("Startup failed: %s", exc)
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
