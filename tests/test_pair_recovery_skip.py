"""Explicit local skipping preserves receipts and never requests exchange data."""
from contextlib import ExitStack
from copy import deepcopy
import sqlite3
from unittest import TestCase
from unittest.mock import patch

from tests import test_pair_manual_baseline as baseline
from tests import test_pair_member_budget as members
from tests import test_pair_order_recovery as recovery
from trading.models import TradingError, dec
from trading.pair_execution import PairTrader
from trading.store import Store


class PairRecoverySkipTests(TestCase):
    setUp = baseline.PairManualBaselineTests.setUp
    seed = baseline.PairManualBaselineTests.seed
    state = baseline.PairManualBaselineTests.state
    broker = baseline.PairManualBaselineTests.broker
    no_writes = baseline.PairManualBaselineTests.no_writes

    def skip(self, batch_id=recovery.BATCH_ID):
        with self.no_writes(), ExitStack() as stack:
            for key in ('long', 'short'):
                for method in ('query', 'snapshot'):
                    stack.enter_context(patch.object(self.broker(key), method,
                        side_effect=AssertionError('skip must not query exchange')))
            return self.engine.pairs.skip_recovery('gold', batch_id, True)

    def test_local_skip_records_only_known_fills_and_keeps_manual_positions(self):
        original = self.seed()
        for key in ('long', 'short'):
            broker = self.broker(key)
            broker.state['positions']['XAUUSD1:' + key.upper()]['qty'] = '12'
            broker.save()
        expected = deepcopy(original)
        PairTrader._account_volume(expected, original['pending'])
        result = self.skip()
        self.assertFalse(result['positions_verified'])
        state = self.state()
        self.assertIsNone(state['pending'])
        self.assertEqual(state['phase'], 'paused')
        self.assertEqual(state['owned'], original['owned'])
        self.assertEqual(state['progress'], original['progress'])
        self.assertEqual(state['daily_volume'], expected['daily_volume'])
        self.assertFalse(self.f.store.pair('gold')['enabled'])
        audit = self.f.store.get('pair_order_recovery:gold:' + recovery.BATCH_ID)
        self.assertEqual(audit['pending'], original['pending'])
        self.assertFalse(audit['positions_verified'])
        batch = self.f.store.get('pair_batch:' + recovery.BATCH_ID)
        self.assertEqual(batch['legs'], original['pending']['legs'])
        self.assertEqual(batch['resolution'], 'manual_skip')
        self.assertNotIn('已核实', state['reason'])
        with self.no_writes():
            self.engine.pairs.enable('gold', True)
        self.assertEqual(self.state()['owned'], {'LONG': '12', 'SHORT': '12'})
        for key in ('long', 'short'):
            self.assertEqual(dec(self.broker(key).state['positions']['XAUUSD1:' + key.upper()]['qty']), 12)
        self.assertEqual(self.state()['daily_volume'], expected['daily_volume'])

    def test_repeated_request_cannot_count_receipts_twice(self):
        self.seed()
        self.skip()
        state = self.state()
        with self.assertRaises(TradingError):
            self.skip()
        self.assertEqual(self.state(), state)

    def test_unknown_active_invalid_and_missing_receipts_retain_batch(self):
        original = self.seed()
        for receipt in (None, recovery.receipt_for(original['pending']['legs'][0], status='NEW'),
                        recovery.receipt_for(original['pending']['legs'][0], client_id='wrong'), {}):
            value = deepcopy(original)
            value['pending']['legs'][0]['receipt'] = receipt
            self.f.store.put('pair_runtime:gold', value)
            with self.subTest(receipt=receipt), self.assertRaises(TradingError):
                self.skip()
            self.assertEqual(self.state(), value)

    def test_unknown_repair_blocks_but_terminal_repair_is_archived(self):
        original = self.seed()
        order = deepcopy(original['pending']['legs'][1]['order'])
        order.update(side='BUY', newClientOrderId='repair-short', quantity='0.1')
        repair = {'key': 'short', 'order': order, 'receipt': None, 'dispatch': 'sending'}
        original['pending']['repairs'] = [repair]
        original['pending']['repair_attempts'] = 1
        self.f.store.put('pair_runtime:gold', original)
        with self.assertRaises(TradingError):
            self.skip()
        repair['receipt'] = recovery.receipt_for(repair)
        self.f.store.put('pair_runtime:gold', original)
        self.skip()
        self.assertEqual(self.f.store.get('pair_batch:' + recovery.BATCH_ID)['repairs'], [repair])

    def test_wrong_batch_and_unacknowledged_skip_are_rejected(self):
        original = self.seed()
        with self.assertRaises(TradingError):
            self.skip('stale-batch')
        for ack in (False, None, 'true', 1):
            with self.subTest(ack=ack), self.assertRaises(TradingError):
                self.engine.pairs.skip_recovery('gold', recovery.BATCH_ID, ack)
        self.assertEqual(self.state(), original)

    def test_running_cycle_transfer_and_identity_changes_block(self):
        original = self.seed()
        pair = self.f.store.pair('gold')
        self.f.store.save_pair({**pair, 'enabled': True})
        with self.assertRaises(TradingError):
            self.skip()
        self.f.store.save_pair({**self.f.store.pair('gold'), 'enabled': False})
        for change in ('cycle', 'close', 'leverage', 'identity', 'cycle_quantity'):
            state = deepcopy(original)
            if change in ('cycle', 'leverage'):
                state['pending']['kind'] = change
            elif change == 'close':
                state['pending']['phase'] = change
            elif change == 'identity':
                state['pending']['identities'] = {}
            else:
                state['progress']['quantities']['LONG'] = '1'
            self.f.store.put('pair_runtime:gold', state)
            with self.subTest(change=change), self.assertRaises(TradingError):
                self.skip()
            self.assertEqual(self.state(), state)
        self.f.store.put('pair_runtime:gold', original)
        self.f.store.put('pair_margin:gold', {'status': 'unknown', 'pending': {'id': 'transfer'}})
        with self.assertRaises(TradingError):
            self.skip()
        self.assertEqual(self.state(), original)

    def test_existing_watch_survives_and_wait_flags_are_cleared(self):
        original = self.seed()
        original.update(recovery_watch={'batches': [{'id': 'older'}]}, attention='old',
                        api_notice={'kind': 'budget'}, retry_after=10, retry_at=9999999999, failure_count=4)
        self.f.store.put('pair_runtime:gold', original)
        self.skip()
        self.assertEqual(self.state()['recovery_watch'], original['recovery_watch'])
        for key in ('attention', 'api_notice', 'retry_after', 'retry_at', 'failure_count'):
            self.assertNotIn(key, self.state())

    def test_database_failure_rolls_back_audit_volume_and_pending_together(self):
        original = self.seed()
        with self.f.store.connect() as db:
            db.execute("CREATE TRIGGER fail_skip BEFORE INSERT ON kv WHEN NEW.key='pair_batch:" +
                       recovery.BATCH_ID + "' BEGIN SELECT RAISE(ABORT, 'skip history failure'); END")
        with self.assertRaisesRegex(sqlite3.DatabaseError, 'skip history failure'):
            self.skip()
        self.assertEqual(self.state(), original)
        self.assertIsNone(self.f.store.get('pair_order_recovery:gold:' + recovery.BATCH_ID))

    def test_other_store_changes_cannot_be_overwritten(self):
        original = self.seed()
        other = Store(self.f.store.path)
        commit = self.f.store.commit_pair_recovery
        changed = {**original, 'reason': 'newer state'}
        def race(*args, **kwargs):
            other.put('pair_runtime:gold', changed)
            return commit(*args, **kwargs)
        with patch.object(self.f.store, 'commit_pair_recovery', side_effect=race), self.assertRaises(TradingError):
            self.skip()
        self.assertEqual(self.state(), changed)
        self.assertIsNone(self.f.store.get('pair_order_recovery:gold:' + recovery.BATCH_ID))


class PairRecoverySkipLiveTests(TestCase):
    setUp = baseline.PairManualBaselineLiveTests.setUp
    tearDown = baseline.PairManualBaselineLiveTests.tearDown
    seed = baseline.PairManualBaselineLiveTests.seed
    budgets = members.PairMemberBudgetTests.budgets

    def test_zero_api_weight_even_with_exhausted_budget_and_exchange_cooldown(self):
        self.seed()
        budget = self.budgets()['test']
        with budget.reconciliation():
            budget.reserve(1800)
        budget.block(30, reason='HTTP 429')
        with ExitStack() as stack:
            for broker in self.live.values():
                broker.api.calls.clear()
                stack.enter_context(patch.object(broker.api, 'call', side_effect=AssertionError('zero HTTP')))
            result = self.engine.pairs.skip_recovery('gold', recovery.BATCH_ID, True)
        self.assertEqual(result['status'], 'skipped')
        self.assertEqual(budget.snapshot()['used'], 1800)
        self.assertTrue(all(not broker.api.calls for broker in self.live.values()))
