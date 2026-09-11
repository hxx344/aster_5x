"""Upgrades below the opening floor must not authorize new positions."""
import time
import unittest
from unittest.mock import patch

from trading.engine import Engine
from trading.models import Book, SYMBOLS, dec
from trading.store import Store
from .helpers import Fixture


XAU, SPCX, CL = SYMBOLS


class UpgradeOpeningFloorTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.addCleanup(self.f.close)
        self.f.account['policy'].update(symbols=list(SYMBOLS), min_open_leverage=5, margin_limit='0.9')
        self.f.store.save_account(self.f.account)
        self.f.broker.state['wallet'] = '100000'
        self.f.broker.state['leverages'] = {XAU: 5, SPCX: 5, CL: 1}
        for symbol, qty, entry in ((XAU, '43.516', '4350.77'), (CL, '1.018', '98.28')):
            for side in ('LONG', 'SHORT'):
                self.f.broker.state['positions'][symbol + ':' + side] = {'qty': qty, 'entry': entry}
        self.f.broker.save()
        quotes = {XAU: ('4350.77', '.000002'), SPCX: ('100', '.000335'), CL: ('98.28', '.000204')}

        def quote(symbol):
            mark, spread = map(dec, quotes[symbol])
            return Book(mark * (1 - spread / 2), mark * (1 + spread / 2),
                        dec(100), dec(100), mark, time.time())

        patcher = patch.object(self.f.market, 'book', side_effect=quote)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.engine = Engine(self.f.store, market=self.f.market)
        self.engine.brokers['test'] = self.f.broker

    def publish(self, cl_capacities):
        for symbol in SYMBOLS:
            self.engine.poll_market(symbol)
            self.engine.markets[symbol]['capacities'] = {
                str(tier): str(value) for tier, value in (cl_capacities if symbol == CL else {}).items()}

    def test_cl_upgrades_below_floor_then_opens_only_after_reaching_floor(self):
        self.publish({4: 68360})
        with patch.object(self.f.broker, 'set_leverage', wraps=self.f.broker.set_leverage) as change, \
             patch.object(self.f.broker, 'submit', wraps=self.f.broker.submit) as submit:
            self.engine.tick_account('test')
            change.assert_called_once_with(CL, 4)
            self.assertEqual(self.f.store.intent('test')['target'], 4)
            self.engine.tick_account('test')  # Confirm actual 4x.
            self.assertIsNone(self.f.store.intent('test'))
            self.engine.tick_account('test')  # 4x capacity cannot authorize entry below 5x.
            submit.assert_not_called()
            self.assertIn('低于 5x', self.engine.views['test']['strategies'][CL]['reason'])
            self.assertEqual([p.qty for p in self.f.broker.snapshot(SYMBOLS).pair(CL)], [dec('1.018')] * 2)
            self.publish({4: 68360, 5: 68360})
            self.engine.tick_account('test')
            self.assertEqual(change.call_args.args, (CL, 5))
            self.engine.tick_account('test')  # Confirm actual 5x before entry.
            submit.assert_not_called()
            self.engine.tick_account('test')
            submit.assert_called_once()
            self.assertEqual({order['symbol'] for order in submit.call_args.args[0]}, {CL})

    def test_all_base_tiers_remain_available_below_custom_opening_floor(self):
        self.f.account['policy']['min_open_leverage'] = 125
        self.f.store.save_account(self.f.account)
        with patch.object(self.f.broker, 'set_leverage', wraps=self.f.broker.set_leverage) as change, \
             patch.object(self.f.broker, 'submit', side_effect=AssertionError('must not open below 125x')):
            for tier in (4, 5, 10, 20):
                self.publish({tier: 68360})
                self.engine.tick_account('test')
                self.assertIsNotNone(self.f.store.intent('test'))
                self.assertEqual(change.call_args.args, (CL, tier))
                self.engine.tick_account('test')
                self.assertIsNone(self.f.store.intent('test'))
                self.engine.tick_account('test')
            self.assertEqual(change.call_count, 4)

    def test_scheduler_budgets_for_upgrade_below_opening_floor(self):
        row = {**self.f.account, 'mode': 'live'}
        self.engine.view('test', snapshot={'positions': [
            {'symbol': symbol, 'leverage': 1 if symbol == CL else 5, 'qty': '0', 'side': side}
            for symbol in SYMBOLS for side in ('LONG', 'SHORT')]})
        self.publish({4: 0})
        idle_gap = self.engine.scheduling([row])['test']['gap']
        self.publish({4: 68360})
        self.assertGreater(self.engine.scheduling([row])['test']['gap'], idle_gap)

    def test_restart_confirms_upgrade_without_opening_below_floor(self):
        self.publish({4: 68360})
        self.engine.tick_account('test')
        self.assertIsNotNone(self.f.store.intent('test'))
        self.engine = Engine(Store(self.f.store.path), market=self.f.market)
        self.engine.brokers['test'] = self.f.broker
        self.publish({4: 68360})
        with patch.object(self.f.broker, 'set_leverage', side_effect=AssertionError('must not repeat upgrade')), \
             patch.object(self.f.broker, 'submit', side_effect=AssertionError('must not open below floor')):
            self.engine.tick_account('test')
            self.assertIsNone(self.f.store.intent('test'))
            self.engine.tick_account('test')
        self.assertTrue(self.f.store.account('test')['enabled'])
        self.assertEqual([p.leverage for p in self.f.broker.snapshot(SYMBOLS).pair(CL)], [4, 4])


if __name__ == '__main__':
    unittest.main()
