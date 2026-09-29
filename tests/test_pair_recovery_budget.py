"""A persisted review stage fits the reserve without repeated account reads."""
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_manual_baseline as baseline
from tests import test_pair_member_budget as members
from trading.exchange import RateBudget
from trading.pair_execution import PairTrader


class PairRecoveryBudgetTests(TestCase):
    setUp = baseline.PairManualBaselineLiveTests.setUp
    tearDown = baseline.PairManualBaselineLiveTests.tearDown
    seed = baseline.PairManualBaselineLiveTests.seed
    state = baseline.PairManualBaselineLiveTests.state
    broker = baseline.PairManualBaselineLiveTests.broker
    no_writes = baseline.PairManualBaselineLiveTests.no_writes
    budgets = members.PairMemberBudgetTests.budgets

    def prepare(self, *, review=False):
        original = self.seed()
        if review:
            original['pending']['position_review'] = True
            self.f.store.put('pair_runtime:gold', original)
        for broker in self.live.values():
            broker.api.calls.clear()
            broker.cached_at.clear()
        return original, self.budgets()['test']

    def check(self):
        with self.no_writes():
            return self.engine.pairs.check_recovery('gold')

    def test_cold_retry_after_restart_completes_inside_300_reserve(self):
        original, budget = self.prepare()
        budget.reserve(1500)
        result = self.check()
        self.assertFalse(result['completed'])
        self.assertTrue(self.state()['pending']['position_review'])
        self.assertEqual(budget.snapshot()['used'], 1642)
        self.assertTrue(all(not any(call[1].endswith('/openOrders') for call in broker.api.calls)
                            for broker in self.live.values()))
        # A new process/window retains only the durable review stage.
        budget = RateBudget()
        budget.reserve(1500)
        for broker in self.live.values():
            broker.api.budget = budget
            broker.api.calls.clear()
            broker.cached_at.clear()
        self.engine.pairs.trader = PairTrader(self.engine)
        result = self.check()
        self.assertTrue(result['completed'], result)
        self.assertEqual(budget.snapshot()['used'], 1722)
        for broker in self.live.values():
            paths = [call[1] for call in broker.api.calls]
            self.assertEqual(paths.count('/fapi/v3/accountWithJoinMargin'), 1)
            self.assertEqual(paths.count('/fapi/v3/openOrders'), 1)
        self.assertEqual(self.state()['owned'], original['owned'])

    def test_review_with_1724_used_does_not_spend_any_partial_read(self):
        original, budget = self.prepare(review=True)
        with budget.reconciliation():
            budget.reserve(1724)
        result = self.check()
        self.assertFalse(result['completed'])
        self.assertEqual(budget.snapshot()['used'], 1724)
        self.assertTrue(all(not broker.api.calls for broker in self.live.values()))
        self.assertEqual(self.state()['pending'], original['pending'])

    def test_review_flag_does_not_replace_unknown_order_query(self):
        original, budget = self.prepare(review=True)
        original['pending']['legs'][0]['receipt'] = None
        self.f.store.put('pair_runtime:gold', original)
        with patch.object(self.engine.pairs, '_read_members', side_effect=AssertionError('unknown order')):
            result = self.check()
        self.assertFalse(result['completed'])
        self.assertIsNotNone(self.state()['pending'])
        self.assertIn('/fapi/v3/order', [call[1] for call in self.broker().api.calls])

    def test_review_still_rejects_foreign_open_orders(self):
        original, budget = self.prepare(review=True)
        self.broker().api.responses['/fapi/v3/openOrders'] = [{'symbol': 'SPCXUSD1'}]
        self.assertFalse(self.check()['completed'])
        self.assertEqual(self.state()['owned'], original['owned'])
        self.assertIsNotNone(self.state()['pending'])

    def test_changed_positions_use_same_full_read_for_existing_repair_decision(self):
        from tests.test_cycle_account_snapshot import ACCOUNT, RISK, risk_row
        original, budget = self.prepare(review=True)
        rows = self.broker('short').api.responses[ACCOUNT]['positions']
        for row in rows:
            if row['positionSide'] == 'SHORT':
                row['positionAmt'] = '-0.9'
        self.broker('short').api.responses[RISK] = [risk_row(row) for row in rows]
        result = self.check()
        self.assertFalse(result['completed'])
        self.assertEqual(self.state()['phase'], 'repairing')
        self.assertEqual(self.state()['pending']['repairs'], [])
        self.assertEqual(budget.snapshot()['used'], 222)
        self.assertEqual(self.state()['owned'], original['owned'])
