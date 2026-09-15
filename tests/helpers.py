from pathlib import Path
import tempfile
import time

from trading.engine import DEFAULT_POLICY, Engine
from trading.paper import DemoMarket, PaperBroker
from trading.store import Store


def seed_cycle_capacity(engine, *, now=None):
    """Explicit public-cache prerequisite for tests of other cycle conditions."""
    now = time.time() if now is None else now
    symbols = {a.get("cycle", {}).get("symbol", "XAUUSD1") for a in engine.store.accounts()}
    for symbol in symbols:
        engine.markets[symbol] = {"status": "ok", "checked_at": now,
                                  "capacities": {str(v): "1000000" for v in range(1, 21)}}


def account(account_id="test", mode="paper"):
    return {"id": account_id, "name": "测试子账户", "mode": mode, "env_prefix": "ASTER_" + account_id.upper(),
            "enabled": True, "policy": {**DEFAULT_POLICY, "symbols": ["XAUUSD1"]}}


class Fixture:
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.directory.name) / "state.sqlite3")
        self.account = account()
        self.store.save_account(self.account)
        self.market = DemoMarket()
        self.broker = PaperBroker("test", self.market, self.store)

    def close(self):
        self.directory.cleanup()
