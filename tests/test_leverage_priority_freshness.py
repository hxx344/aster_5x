"""Validate high-tier quota after the last live account read, before any POST."""
import copy
import json
import threading
import time
import unittest
from unittest.mock import patch

import httpx

from trading.engine import Engine
from trading.exchange import API, LiveBroker, RateBudget
from trading.models import SYMBOLS, dec
from .helpers import Fixture
from .test_exchange_hardening import account_responses


SYMBOL = "XAUUSD1"


class LivePriorityFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.responses, self.calls = account_responses(), []
        self.responses["/fapi/v3/leverage"] = {"symbol": SYMBOL, "leverage": 10}
        self.on_account_read = None

        def handle(request):
            self.calls.append(request.url.path)
            if request.url.path == "/fapi/v3/accountWithJoinMargin" and self.on_account_read:
                self.on_account_read(self.calls.count(request.url.path))
            return httpx.Response(200, json=copy.deepcopy(self.responses[request.url.path]))

        self.api = API(transport=httpx.MockTransport(handle), budget=RateBudget())
        self.api.signed_parameters = lambda params: params
        self.addCleanup(self.api.close)
        self.broker = LiveBroker({}, self.f.market, api=self.api)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.broker
        with patch.object(self.f.market, "capacities", return_value={10: dec(500000)}):
            self.engine.poll_market(SYMBOL)

    def priority_tick(self):
        self.engine.priority_accounts.pop("test", None)
        self.engine.active_priority_accounts.add("test")
        return self.engine.tick_account("test")

    def assert_aborted_without_write(self, reason):
        self.assertNotIn("/fapi/v3/leverage", self.calls)
        self.assertIsNone(self.f.store.intent("test"))
        self.assertTrue(self.f.store.account("test")["enabled"])
        with self.f.store.connect() as db:
            row = db.execute("SELECT data FROM intents ORDER BY rowid DESC LIMIT 1").fetchone()
        self.assertIsNotNone(row, "the locally aborted intent should remain auditable")
        intent = json.loads(row[0])
        self.assertEqual(intent["status"], "aborted")
        self.assertIn(reason, intent["last_error"])
        self.assertNotIn("change_response", intent)
        self.assertNotIn("submission_error", intent)

    def expire_checked_snapshot_during_intent_commit(self):
        original = self.f.store.save_intent

        def commit(intent):
            original(intent)
            if intent["status"] == "pending" and self.broker.leverage_snapshot:
                snapshot, _ = self.broker.leverage_snapshot
                self.broker.leverage_snapshot = (snapshot, time.monotonic() - 2)

        return patch.object(self.f.store, "save_intent", side_effect=commit)

    def test_capacity_drop_during_broker_fallback_read_aborts_without_post(self):
        def drop_on_fallback(count):
            if count == 3:
                self.engine.markets[SYMBOL]["capacities"]["10"] = "0"

        self.on_account_read = drop_on_fallback
        with self.expire_checked_snapshot_during_intent_commit():
            self.priority_tick()
        self.assertEqual(self.calls.count("/fapi/v3/accountWithJoinMargin"), 3)
        self.assert_aborted_without_write("目标杠杆额度")

    def test_capacity_expiry_during_broker_fallback_read_aborts_without_post(self):
        def expire_on_fallback(count):
            if count == 3:
                self.engine.markets[SYMBOL]["checked_at"] = time.time() - 9

        self.on_account_read = expire_on_fallback
        with self.expire_checked_snapshot_during_intent_commit():
            self.priority_tick()
        self.assertEqual(self.calls.count("/fapi/v3/accountWithJoinMargin"), 3)
        self.assert_aborted_without_write("市场额度快照过期")

    def test_slow_validation_quote_cannot_post_with_an_expired_account_snapshot(self):
        wall = [time.time()]
        book = self.f.market.book
        quote_reads = []

        def slow_quote(symbol):
            quote_reads.append(symbol)
            if len(quote_reads) == 2:
                wall[0] += 9
                # The independent public feed stays fresh while the quote blocks.
                self.engine.markets[SYMBOL]["checked_at"] = wall[0]
            return book(symbol)

        with patch("trading.exchange.time.time", side_effect=lambda: wall[0]), \
             patch.object(self.f.market, "book", side_effect=slow_quote):
            self.priority_tick()
        self.assertEqual(len(quote_reads), 2)
        self.assert_aborted_without_write("账户快照")

    def test_fresh_validation_still_posts_once_without_an_extra_account_read(self):
        self.priority_tick()
        self.assertEqual(self.calls.count("/fapi/v3/accountWithJoinMargin"), 2)
        self.assertEqual(self.calls.count("/fapi/v3/leverage"), 1)
        self.assertEqual(self.f.store.intent("test")["target"], 10)


class PriorityOpportunityProgressTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers["test"] = self.f.broker

    def test_low_tier_first_add_marker_does_not_consume_a_new_high_tier_opportunity(self):
        self.f.broker.state["leverages"][SYMBOL] = 5
        self.f.broker.save()
        self.f.store.put("open_after_leverage:test:" + SYMBOL, 5)
        with patch.object(self.f.market, "capacities", return_value={5: dec(500000), 10: dec(500000)}):
            self.engine.poll_market(SYMBOL)
        self.engine.active_priority_signals["test"] = self.engine.priority_accounts.pop("test")
        self.engine.active_priority_accounts.add("test")
        with patch.object(self.f.broker, "submit", wraps=self.f.broker.submit) as submit:
            self.engine.tick_account("test")
        submit.assert_not_called()
        self.assertEqual(self.f.store.intent("test")["target"], 10)
        self.assertEqual(self.f.broker.state["leverages"][SYMBOL], 10)

    def test_simultaneous_market_signals_finish_once_without_repeated_private_work(self):
        self.f.account["policy"]["symbols"] = list(SYMBOLS)
        self.f.store.save_account(self.f.account)
        initial, completed, excess = threading.Event(), threading.Event(), threading.Event()
        calls = []
        real_tick, real_poll = self.engine.tick_account, self.engine.poll_market

        def tick(account_id):
            calls.append(account_id)
            if len(calls) == 1:
                initial.set()
                return 60
            if len(calls) > 16:
                excess.set()
                self.engine.shutdown.set()
                return 60
            result = real_tick(account_id)
            opened = {order["symbol"] for order in self.f.broker.state["orders"].values()}
            if (opened == set(SYMBOLS) and not self.f.store.intent(account_id)
                    and all(self.f.broker.state["leverages"][symbol] == 10 for symbol in SYMBOLS)):
                completed.set()
            return result

        timing = {"test": {"interval": 60, "gap": 60}}
        with patch.object(self.engine, "scheduling", return_value=timing), \
             patch.object(self.engine, "tick_account", side_effect=tick), \
             patch.object(self.engine, "poll_market", return_value=60), \
             patch.object(self.engine, "poll_book", return_value=60), \
             patch.object(self.engine, "notify", return_value=60), \
             patch.object(self.f.market, "capacities", return_value={10: dec(500000)}):
            self.engine.start()
            try:
                self.assertTrue(initial.wait(1))
                # Publish the complete batch before the scheduler consumes any edge.
                with self.engine.lock:
                    for symbol in SYMBOLS:
                        real_poll(symbol)
                finished = completed.wait(5)
                self.assertTrue(finished, f"market opportunities did not finish: calls={len(calls)}, "
                    f"leverages={self.f.broker.state['leverages']}, "
                    f"orders={list(self.f.broker.state['orders'].values())}, "
                    f"signals={self.engine.priority_accounts}, followups={self.engine.priority_followups}, "
                    f"reason={self.engine.views.get('test', {}).get('reason')}")
                self.assertFalse(excess.is_set(), "priority signals caused repeated private account work")
                self.assertEqual(len(self.f.broker.state["orders"]), 2 * len(SYMBOLS))
                before = len(calls)
                for symbol in SYMBOLS:
                    real_poll(symbol)
                self.assertFalse(excess.wait(.3))
                self.assertEqual(len(calls), before, "unchanged availability restarted completed opportunities")
                self.assertFalse(self.engine.priority_accounts)
                self.assertFalse(self.engine.priority_followups)
            finally:
                self.engine.stop()


if __name__ == "__main__":
    unittest.main()
