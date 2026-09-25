"""Display-only observations of ordinary quota availability and order dispatch."""
import math
import time
from contextlib import contextmanager

from .request_timing import observe_transport


def valid_time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def initial_timing(symbol, leverage, observation):
    observation = observation if isinstance(observation, dict) else None
    return {"symbol": symbol, "leverage": leverage,
            "detected_at": observation.get("detected_at") if observation else None,
            "threshold": observation.get("threshold") if observation else None,
            "submitted_at": None, "latency_ms": None, "status": "unrecorded"}


@contextmanager
def observe_capacity_submit(timing, observation, *, live):
    """Measure to HTTP entry, excluding response latency and any later repairs."""
    from .exchange import RequestNotSent
    if callable(observation):
        try:
            observation = observation()
        except Exception:
            # Diagnostics cannot prevent reconciliation or authorize a trade.
            observation = None
    observation = observation if isinstance(observation, dict) else None
    timing.update(initial_timing(timing["symbol"], timing["leverage"], observation))
    transport, started, started_at = {}, time.monotonic(), time.time()
    not_sent = False
    try:
        with observe_transport(transport):
            yield
    except RequestNotSent:
        not_sent = True
        raise
    finally:
        if not_sent:
            timing.update(status="not_sent", submitted_at=None, latency_ms=None)
        else:
            sent_at = transport.get("http_started_at") if live else started_at
            offset = transport.get("before_http_ms") if live else 0
            detected = observation.get("detected_tick") if observation else None
            if valid_time(sent_at):
                timing["submitted_at"] = sent_at
            if (valid_time(detected) and valid_time(offset) and valid_time(sent_at)
                    and started >= detected):
                delay = (started - detected) * 1000 + offset
                if math.isfinite(delay):
                    timing.update(status="measured" if live else "paper", latency_ms=round(delay, 1))


def capacity_timing_text(intent):
    timing = intent.get("capacity_timing")
    if not isinstance(timing, dict):
        timing = {}
    delay = timing.get("latency_ms")
    if timing.get("status") in ("measured", "paper") and valid_time(delay):
        boundary = "模拟发单" if timing["status"] == "paper" else "发单"
        return f"；额度达标 → {boundary}：{delay:g} ms（从程序首次检测起）"
    if timing.get("status") == "not_sent":
        return "；额度达标 → 发单：本地未发送"
    return "；额度达标 → 发单：未记录"
