"""Full member checks admit their complete cost before any offline HTTP read."""
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_integration as integration
from trading.exchange import BudgetWait, RateBudget, RequestNotSent
from trading.pairing import validate_pair


class PairMemberBudgetTests(TestCase):
    tearDown = integration.PairSnapshotLifecycleTests.tearDown

    def setUp(self):
        integration.PairSnapshotLifecycleTests.setUp(self)
        self.pair = validate_pair(integration.pair_config())

    def budgets(self, *, shared=True):
        first = RateBudget()
        result = {}
        for account_id, broker in self.live.items():
            budget = first if shared else RateBudget()
            broker.api.budget = result[account_id] = budget
            original = broker.api.call
            broker.api.priorities = []

            def call(method, path, *args, _api=broker.api, _original=original, **kwargs):
                _api.budget.reserve(kwargs.get("weight", 1))
                _api.priorities.append(_api.budget._priority_flags())
                return _original(method, path, *args, **kwargs)

            broker.api.call = call
        return result

    def read(self, *, reconciliation=True):
        return self.engine.pairs._read_members(self.pair, adopt=True, reconciliation=reconciliation)

    def assert_no_reads(self):
        self.assertTrue(all(not broker.api.calls for broker in self.live.values()))

    @staticmethod
    def spend(budget, weight):
        with budget.reconciliation():
            budget.reserve(weight)

    def test_shared_budget_rejects_all_224_weight_before_any_http(self):
        budget = self.budgets()["test"]
        self.spend(budget, 1724)
        with patch.object(budget, "require_available", wraps=budget.require_available) as admission:
            with self.assertRaises(BudgetWait) as raised:
                self.read()
        admission.assert_called_once_with(224)
        self.assertIn("1724/1800", str(raised.exception))
        self.assertIn("224", str(raised.exception))
        self.assertGreater(raised.exception.retry_after, 0)
        self.assertEqual(budget.snapshot()["used"], 1724)
        self.assert_no_reads()

    def test_start_admits_ownership_reads_before_any_account_http(self):
        self.f.store.save_pair(self.pair, create=True)
        budget = self.budgets()["test"]
        budget.configure_capacity_reserve(31)
        budget.reserve(1235)
        with self.assertRaisesRegex(BudgetWait, "239"):
            self.engine.pairs.enable(self.pair["id"], True)
        self.assert_no_reads()
        self.verify.assert_not_called()
        self.assertEqual(budget.snapshot()["used"], 1235)
        self.assertFalse(self.f.store.pair(self.pair["id"])["enabled"])

    def test_start_completes_with_room_for_full_ownership_check(self):
        self.f.store.save_pair(self.pair, create=True)
        budget = self.budgets()["test"]
        budget.configure_capacity_reserve(31)
        budget.reserve(1230)
        self.verify.side_effect = lambda pair: [budget.reserve(5) for _ in range(3)]
        self.engine.pairs.enable(self.pair["id"], True)
        self.assertEqual(budget.snapshot()["used"], 1467)
        self.assertTrue(self.f.store.pair(self.pair["id"])["enabled"])

    def test_shared_recovery_reserve_completes_all_checks_for_222_weight(self):
        budget = self.budgets()["test"]
        budget.reserve(1500)
        with patch.object(budget, "require_available", wraps=budget.require_available) as admission:
            snapshots, guards = self.read()
        admission.assert_called_once_with(224)
        self.assertEqual(set(snapshots), {"long", "short"})
        self.assertEqual(budget.snapshot()["used"], 1722)
        for broker in self.live.values():
            self.assertTrue(all(flags[0] and not flags[1] and not flags[2]
                                for flags in broker.api.priorities))
            paths = [call[1] for call in broker.api.calls]
            self.assertEqual(paths.count("/fapi/v3/positionSide/dual"), 1)
            self.assertEqual(paths.count("/fapi/v3/multiAssetsMargin"), 1)
            orders = [call for call in broker.api.calls if call[1] == "/fapi/v3/openOrders"]
            self.assertEqual(len(orders), 1)
            self.assertEqual(orders[0][2], ())
            self.assertEqual(orders[0][3], {"signed": True, "weight": 40})
        for _, current in guards.values():
            current()

    def test_separate_budgets_admit_only_their_own_112_weight(self):
        budgets = self.budgets(shared=False)
        first, second = budgets["test"], budgets["second"]
        self.spend(first, 1600)
        self.spend(second, 1600)
        with patch.object(first, "require_available", wraps=first.require_available) as first_admission, \
                patch.object(second, "require_available", wraps=second.require_available) as second_admission:
            self.read()
        first_admission.assert_called_once_with(112)
        second_admission.assert_called_once_with(112)
        self.assertEqual(first.snapshot()["used"], 1711)
        self.assertEqual(second.snapshot()["used"], 1711)

    def test_one_blocked_independent_budget_prevents_both_workers(self):
        budgets = self.budgets(shared=False)
        self.spend(budgets["second"], 1724)
        with self.assertRaises(BudgetWait):
            self.read()
        self.assertEqual(budgets["test"].snapshot()["used"], 0)
        self.assert_no_reads()

    def test_ordinary_check_cannot_inherit_the_recovery_reserve(self):
        budget = self.budgets()["test"]
        budget.reserve(1280)
        with budget.reconciliation():
            with self.assertRaises(BudgetWait) as raised:
                self.read(reconciliation=False)
            self.assertEqual(budget._priority_flags(), (True, False, False))
        self.assertIn("1280/1500", str(raised.exception))
        self.assert_no_reads()

    def test_reconciliation_cannot_bypass_total_limit(self):
        budget = self.budgets()["test"]
        self.spend(budget, 1800)
        with self.assertRaises(BudgetWait):
            self.read()
        self.assert_no_reads()

    def test_reconciliation_cannot_bypass_exchange_cooldown(self):
        budget = self.budgets()["test"]
        budget.block(30, reason="HTTP 429")
        with self.assertRaises(RequestNotSent) as raised:
            self.read()
        self.assertIn("HTTP 429", str(raised.exception))
        self.assertGreater(raised.exception.retry_after, 0)
        self.assertEqual(budget.snapshot()["used"], 0)
        self.assert_no_reads()
