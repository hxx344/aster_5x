"""Optional per-call observations; never shared across account worker threads."""
from contextlib import contextmanager
from contextvars import ContextVar
import math
from time import perf_counter, time


_transport = ContextVar("transport_timing", default=None)
_database = ContextVar("database_timing", default=None)


def _clock():
    try:
        return perf_counter()
    except Exception:
        return None


def _elapsed(start):
    try:
        duration = (perf_counter() - start) * 1000
        return duration if math.isfinite(duration) and duration >= 0 else None
    except Exception:
        return None


@contextmanager
def observe_database():
    data = {"lock_wait_ms": 0.0, "connection_ms": 0.0, "connections": 0}
    token = _database.set({"active": True, "data": data})
    try:
        yield data
    finally:
        _database.reset(token)


def database_clock():
    observed = _database.get()
    return _clock() if observed is not None and observed["active"] else None


def database_duration(key, started):
    observed = _database.get()
    if observed is not None and observed["active"]:
        value = _elapsed(started)
        previous = observed["data"][key]
        observed["data"][key] = previous + value if previous is not None and value is not None else None
        if key == "connection_ms":
            observed["data"]["connections"] += 1


def freeze_database():
    observed = _database.get()
    if observed is not None:
        observed["active"] = False


@contextmanager
def observe_transport(data):
    token = _transport.set((data, _clock()))
    try:
        yield
    finally:
        _transport.reset(token)


@contextmanager
def transport_stage(key):
    observed = _transport.get()
    if observed is None:
        yield
        return
    data, overall = observed
    started = _clock()
    if key == "http_ms":
        try:
            data["http_started_at"] = time()
            data["before_http_ms"] = _elapsed(overall)
        except Exception:
            pass
    try:
        yield
    finally:
        duration = _elapsed(started)
        data[key] = duration
        if key == "http_ms":
            try:
                data["http_finished_at"] = time()
            except Exception:
                pass
