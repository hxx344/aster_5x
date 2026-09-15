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
        self.due = 0
        self.closed = False
        self.history = LedgerCache()

    @staticmethod
    def key(account):
        config = account.get("cycle", {})
        return config.get("symbol"), config.get("daily_volume_limit")

    def read(self, accounts, now, *, history_revisions=None):
        with self.lock:
            live_ids = {account["id"] for account in accounts}
            self.entries = {aid: row for aid, row in self.entries.items() if aid in live_ids}
            running = self.worker is not None and self.worker.is_alive()
            start = not self.closed and not running and time.monotonic() >= self.due and bool(accounts)
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
                    "refreshing": bool(running or start), "error": error})
            if start:
                # A slow report is shared by every browser; requests never queue
                # another calculation or wait for this worker to finish.
                self.worker = threading.Thread(target=self._refresh, args=(deepcopy(accounts),),
                                               name="aster-dashboard-reports", daemon=True)
                self.worker.start()
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

    def close(self):
        with self.lock:
            self.closed = True
            worker = self.worker
        if worker is not None:
            worker.join(timeout=5)
