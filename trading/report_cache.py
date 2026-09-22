"""One bounded background reader for dashboard-only historical reports."""
from copy import deepcopy
import logging
import threading
import time

from .ledger_cache import LedgerCache


LOG = logging.getLogger("aster.trading")
REPORT_INTERVAL = 5
REPORT_MAX_AGE = 15


class ReportCache:
    def __init__(self, load):
        self.load = load
        self.lock = threading.Lock()
        self.entries = {}
        self.worker = None
        self.producer = None
        self.wakeup = threading.Event()
        self.due = 0
        self.closed = False
        self.history = LedgerCache()

    @staticmethod
    def key(account):
        config = account.get("cycle", {})
        return config.get("symbol"), config.get("daily_volume_limit")

    def start(self, accounts_reader):
        """Produce reports independently of browser and hub reads."""
        with self.lock:
            if self.closed or self.producer is not None:
                return
            self.producer = threading.Thread(target=self._produce, args=(accounts_reader,),
                                             name="aster-report-producer", daemon=True)
            self.producer.start()

    def _produce(self, accounts_reader):
        while True:
            self.wakeup.clear()
            with self.lock:
                if self.closed:
                    return
            try:
                self.request_refresh(accounts_reader())
            except Exception as exc:
                LOG.warning("Dashboard report scheduling unavailable (%s)", type(exc).__name__)
            with self.lock:
                if self.closed:
                    return
                remaining = self.due - time.monotonic()
                delay = min(REPORT_INTERVAL, remaining) if remaining > 0 else REPORT_INTERVAL
            self.wakeup.wait(delay)

    def _request_locked(self, accounts):
        live_ids = {account["id"] for account in accounts}
        self.entries = {aid: row for aid, row in self.entries.items() if aid in live_ids}
        running = self.worker is not None and self.worker.is_alive()
        if not self.closed and not running and time.monotonic() >= self.due and accounts:
            self.worker = threading.Thread(target=self._refresh, args=(deepcopy(accounts),),
                                           name="aster-dashboard-reports", daemon=True)
            self.worker.start()
            running = True
        return running

    def request_refresh(self, accounts):
        with self.lock:
            self._request_locked(accounts)

    def read(self, accounts, now, *, history_revisions=None):
        with self.lock:
            running = self._request_locked(accounts)
            result = {}
            for account in accounts:
                entry = self.entries.get(account["id"], {})
                if entry.get("key") != self.key(account):
                    entry = {}
                data, stamp = entry.get("data"), entry.get("as_of")
                stale = stamp is None or not 0 <= now - stamp < REPORT_MAX_AGE or now // 86400 != stamp // 86400
                error = entry.get("error")
                selected = data
                if data is not None and history_revisions is not None:
                    selected = {key: value for key, value in data.items() if key not in ("trades", "trades_revision")}
                    if account["id"] in history_revisions:
                        selected["trades_revision"] = data.get("trades_revision")
                        if not data.get("trades_revision") or history_revisions[account["id"]] != data["trades_revision"]:
                            selected["trades"] = data["trades"]
                result[account["id"]] = (deepcopy(selected), {
                    "as_of": stamp, "max_age_seconds": REPORT_MAX_AGE,
                    "status": ("stale" if stale or error else "ready") if data is not None else ("error" if error else "loading"),
                    "refreshing": running, "error": error})
            return result

    def _refresh(self, accounts):
        try:
            for account in accounts:
                with self.lock:
                    if self.closed:
                        break
                stamp = time.time()
                key = self.key(account)
                try:
                    data = self.load(account, stamp)
                except Exception as exc:
                    LOG.warning("Dashboard report unavailable (%s)", type(exc).__name__)
                    with self.lock:
                        previous = self.entries.get(account["id"], {})
                        if previous.get("key") != key:
                            previous = {"key": key}
                        self.entries[account["id"]] = {**previous, "error": "成交统计暂不可用，正在重新读取"}
                else:
                    with self.lock:
                        self.entries[account["id"]] = {"key": key, "data": data, "as_of": stamp, "error": None}
        finally:
            with self.lock:
                self.due = time.monotonic() + REPORT_INTERVAL
            self.wakeup.set()

    def close(self):
        with self.lock:
            self.closed = True
            producer = self.producer
            worker = self.worker
        self.wakeup.set()
        if producer is not None and producer is not threading.current_thread():
            producer.join(timeout=5)
        if worker is not None:
            worker.join(timeout=5)
