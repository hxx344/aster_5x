"""Bounded derived data, valid only for an exact ledger revision and time range."""
from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import threading


@dataclass(frozen=True)
class _Entry:
    revision: int
    since: float
    until: float
    value: object


class LedgerCache:
    def __init__(self, limit=64):
        self._limit = limit
        self._entries = OrderedDict()
        self._lock = threading.Lock()

    def read(self, key, revision, now):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.revision != revision or not entry.since <= now < entry.until:
                return None
            self._entries.move_to_end(key)
        return deepcopy(entry.value)

    def save(self, key, revision, now, until, value):
        entry = _Entry(revision, now, until, deepcopy(value))
        with self._lock:
            self._entries[key] = entry
            self._entries.move_to_end(key)
            while len(self._entries) > self._limit:
                self._entries.popitem(last=False)
