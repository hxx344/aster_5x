import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  pairOrderRecoveryBlock,
  pairOrderRecoveryReviewBlock,
  pairOrderRecoverySkipBlock,
  parsePairOrderRecovery,
  parsePairOrderRecoveryCheck,
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

function checkResult(changes = {}) {
  return {
    status: 'checked',
    pair_id: 'pair-a',
    batch_id: 'batch-a',
    checked_at: 1000,
    completed: false,
    message: '一笔仍待核对',
    archive_available: false,
    archive_reason: '已有一笔回执',
    orders: [
      {
        side: 'LONG',
        client_order_id: 'original-a',
        status: 'EXPIRED',
        executed_qty: '0',
        error: '',
      },
      {
        side: 'SHORT',
        client_order_id: 'original-b',
        status: 'UNKNOWN',
        executed_qty: null,
        error: '本地 API 请求权重预算不足',
      },
    ],
    ...changes,
  };
}

test('manual check supports mixed receipts and displays budget feedback without an archive token', () => {
  const data = checkResult();
  assert.deepEqual(
    parsePairOrderRecoveryCheck({ ...data, token: 'never-confirm-this' }, pair),
    data,
  );
  assert.equal(
    parsePairOrderRecoveryCheck(data, pair).orders[1].error,
    '本地 API 请求权重预算不足',
  );
  assert.throws(() => parsePairOrderRecovery(data, pair), /状态无效/);
});

test('manual check validates batch binding, completion flags and receipt rows', () => {
  const valid = checkResult();
  assert.equal(
    parsePairOrderRecoveryCheck(checkResult({ completed: true }), pair)
      .completed,
    true,
  );
  for (const data of [
    null,
    {},
    checkResult({ pair_id: 'other' }),
    checkResult({ batch_id: 'other' }),
    checkResult({ completed: 'true' }),
    checkResult({ completed: true, archive_available: true }),
    checkResult({ checked_at: NaN }),
    checkResult({ checked_at: 0 }),
    checkResult({ orders: [] }),
    checkResult({ orders: [valid.orders[0], valid.orders[0]] }),
    checkResult({
      orders: [valid.orders[0], { ...valid.orders[1], status: 'MISSING' }],
    }),
    checkResult({
      orders: [valid.orders[0], { ...valid.orders[1], executed_qty: 'NaN' }],
    }),
    checkResult({
      orders: [valid.orders[0], { ...valid.orders[1], executed_qty: 0 }],
    }),
    checkResult({
      orders: [valid.orders[0], { ...valid.orders[1], error: null }],
    }),
  ])
    assert.throws(() => parsePairOrderRecoveryCheck(data, pair));
});

test('an existing repair order can be displayed without merging it with the original order', () => {
  const valid = checkResult();
  const data = checkResult({
    orders: [
      ...valid.orders,
      { ...valid.orders[0], client_order_id: 'repair-a' },
    ],
  });
  assert.equal(parsePairOrderRecoveryCheck(data, pair).orders.length, 3);
  const repeatedBudgetRejections = checkResult({
    orders: [
      ...valid.orders,
      ...Array.from({ length: 9 }, (_, index) => ({
        ...valid.orders[0],
        client_order_id: `budget-repair-${index}`,
      })),
    ],
  });
  assert.equal(
    parsePairOrderRecoveryCheck(repeatedBudgetRejections, pair).orders.length,
    11,
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

test('local skip is available without a preview and despite budget errors', () => {
  const terminal = {
    ...pair,
    state: {
      api_notice: { kind: 'budget', text: '1788/1800' },
      pending: {
        ...pair.state.pending,
        legs: [
          { key: 'long', receipt: { status: 'REJECTED' } },
          { key: 'short', receipt: { status: 'FILLED' } },
        ],
        repairs: [{ receipt: { status: 'EXPIRED' } }],
      },
      progress: { quantities: { LONG: '0', SHORT: '0' } },
    },
  };
  assert.equal(pairOrderRecoverySkipBlock(terminal), '');
  for (const receipt of [
    null,
    {},
    { status: 'NEW' },
    { status: 'PARTIALLY_FILLED' },
  ]) {
    assert.match(
      pairOrderRecoverySkipBlock({
        ...terminal,
        state: {
          ...terminal.state,
          pending: { ...terminal.state.pending, repairs: [{ receipt }] },
        },
      }),
      /未知或活动/,
    );
  }
  assert.match(
    pairOrderRecoverySkipBlock({ ...terminal, enabled: true }),
    /暂停/,
  );
  assert.match(
    pairOrderRecoverySkipBlock({
      ...terminal,
      state: { ...terminal.state, margin: { status: 'unknown' } },
    }),
    /划转/,
  );
  assert.match(
    pairOrderRecoverySkipBlock({
      ...terminal,
      state: {
        ...terminal.state,
        progress: { quantities: { LONG: '0.1', SHORT: '0' } },
      },
    }),
    /循环/,
  );
});
