import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  pairOrderRecoveryBlock,
  pairOrderRecoveryReviewBlock,
  parsePairOrderRecovery,
} from '../lib/pair-order-recovery.ts';

const pair = {
  id: 'pair-a',
  enabled: false,
  state: { pending: { id: 'batch-a', kind: 'ordinary', phase: 'open' } },
};
function preview(changes = {}) {
  return {
    status: 'review',
    token: 'review-token',
    pair_id: 'pair-a',
    batch_id: 'batch-a',
    created_at: 900,
    checked_at: 1000,
    before: { LONG: '0.0100', SHORT: '0.0100' },
    actual: { LONG: '0.0100', SHORT: '0.0100' },
    leverage: 5,
    orders: [
      { side: 'LONG', client_order_id: 'original-a', result: 'not_found' },
      { side: 'SHORT', client_order_id: 'original-b', result: 'not_found' },
    ],
    message: '原订单暂未查到，核对后可确认归档',
    ...changes,
  };
}

test('only paused ordinary opening batches allow the recovery entry', () => {
  assert.equal(pairOrderRecoveryBlock(pair), '');
  assert.match(pairOrderRecoveryBlock({ ...pair, enabled: true }), /暂停/);
  for (const pending of [
    undefined,
    null,
    {},
    { id: 'batch-a', kind: 'cycle', phase: 'open' },
    { id: 'batch-a', kind: 'ordinary', phase: 'close' },
    { id: 'batch-a', kind: 'leverage' },
    { id: 'batch-a', kind: 'transfer' },
    { kind: 'ordinary', phase: 'open' },
  ]) {
    assert.notEqual(
      pairOrderRecoveryBlock({ ...pair, state: { pending } }),
      '',
    );
  }
  assert.equal(
    pairOrderRecoveryBlock({
      ...pair,
      state: { ...pair.state, margin: { pending: { status: 'unknown' } } },
    }),
    '',
    'server preview must report the detailed transfer conflict',
  );
});

test('a review preserves original identifiers, decimal precision and server text', () => {
  const data = preview({
    actual: { LONG: '0.0000000000000000001', SHORT: '123456789123456789.1234' },
    message: '<img src=x onerror="alert(1)">',
  });
  assert.deepEqual(parsePairOrderRecovery(data, pair), data);
});

test('different pairs or batches cannot supply or confirm a stale preview', () => {
  for (const data of [
    preview({ pair_id: 'pair-b' }),
    preview({ batch_id: 'batch-b' }),
  ]) {
    assert.throws(() => parsePairOrderRecovery(data, pair), /不符/);
    assert.match(pairOrderRecoveryReviewBlock(data, pair, 1001), /已变化/);
  }
  const changedPair = {
    ...pair,
    state: { pending: { ...pair.state.pending, id: 'batch-b' } },
  };
  assert.match(
    pairOrderRecoveryReviewBlock(preview(), changedPair, 1001),
    /已变化/,
  );
  assert.match(
    pairOrderRecoveryReviewBlock(preview(), { ...pair, enabled: true }, 1001),
    /暂停/,
  );
});

test('confirmation expires at exactly five minutes and refuses invalid clocks', () => {
  assert.equal(pairOrderRecoveryReviewBlock(preview(), pair, 1000), '');
  assert.equal(pairOrderRecoveryReviewBlock(preview(), pair, 1299.999), '');
  for (const now of [1300, 1300.001, 9000, NaN, Infinity, 0, -1]) {
    assert.match(pairOrderRecoveryReviewBlock(preview(), pair, now), /过期/);
  }
});

test('found receipts never become a manual archive token', () => {
  assert.deepEqual(
    parsePairOrderRecovery(
      {
        status: 'receipts_found',
        message: '已恢复真实回执，交由原流程继续核对',
        token: 'must-not-be-kept',
      },
      pair,
    ),
    {
      status: 'receipts_found',
      message: '已恢复真实回执，交由原流程继续核对',
    },
  );
});

test('incomplete or non-missing order evidence cannot render an archive review', () => {
  const valid = preview();
  for (const data of [
    null,
    [],
    {},
    preview({ status: 'unexpected' }),
    preview({ token: '' }),
    preview({ checked_at: '1000' }),
    preview({ checked_at: Infinity }),
    preview({ created_at: 0 }),
    preview({ leverage: 0 }),
    preview({ before: { LONG: '1' } }),
    preview({ actual: { LONG: 'NaN', SHORT: '1' } }),
    preview({ actual: { LONG: 1, SHORT: 1 } }),
    preview({ actual: { LONG: '1\n', SHORT: '1' } }),
    preview({ orders: [] }),
    preview({ orders: [valid.orders[0]] }),
    preview({ orders: [valid.orders[0], valid.orders[0]] }),
    preview({ orders: [valid.orders[0], null] }),
    preview({
      orders: [valid.orders[0], { ...valid.orders[1], result: 'FILLED' }],
    }),
    preview({
      orders: [valid.orders[0], { ...valid.orders[1], client_order_id: '' }],
    }),
  ]) {
    assert.throws(() => parsePairOrderRecovery(data, pair));
  }
});
