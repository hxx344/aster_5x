import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  hasPendingPairOrders,
  pendingOrderDiagnostics,
} from '../lib/pair-order-diagnostics.ts';

function pair(pending) {
  return { id: 'fixture-pair', state: { pending } };
}

function diagnostic(leg, pending = {}) {
  return pendingOrderDiagnostics(
    pair({ kind: 'ordinary', phase: 'open', legs: [leg], ...pending }),
  )[0];
}

function freeze(value) {
  if (value && typeof value === 'object') {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

test('only ordinary and cycle pending batches expose market order diagnostics', () => {
  for (const kind of ['ordinary', 'cycle']) {
    assert.equal(hasPendingPairOrders(pair({ kind })), true);
    assert.deepEqual(pendingOrderDiagnostics(pair({ kind })), []);
  }
  for (const fixture of [
    undefined,
    null,
    [],
    {},
    { state: [] },
    { state: 'invalid' },
    pair(undefined),
    pair(null),
    pair([]),
    pair('ordinary'),
    pair({}),
    pair({ kind: 'leverage', legs: [{ key: 'long' }] }),
    pair({ kind: 'future-kind', legs: [{ key: 'short' }] }),
  ]) {
    assert.equal(hasPendingPairOrders(fixture), false);
    assert.deepEqual(pendingOrderDiagnostics(fixture), []);
  }
});

test('original and repair legs show exact order identifiers, stages and side labels', () => {
  const result = pendingOrderDiagnostics(
    pair({
      kind: 'cycle',
      phase: 'close',
      legs: [
        {
          key: 'long',
          order: { newClientOrderId: 'batch-a' },
          receipt: { status: 'FILLED', executedQty: '0.0100' },
        },
        {
          key: 'short',
          order: { newClientOrderId: 'batch-b' },
          receipt: { status: 'NEW', executedQty: '0' },
        },
      ],
      repairs: [
        {
          key: 'short',
          order: { newClientOrderId: 'repair-b' },
          receipt: { status: 'PARTIALLY_FILLED', executedQty: '0.0050' },
        },
      ],
    }),
  );
  assert.deepEqual(result, [
    {
      id: 'legs:0:batch-a',
      sideLabel: 'A · 只多',
      stageLabel: '本批减仓',
      clientOrderId: 'batch-a',
      statusLabel: '全部成交',
      executedQty: '0.0100',
      error: '',
      terminal: true,
    },
    {
      id: 'legs:1:batch-b',
      sideLabel: 'B · 只空',
      stageLabel: '本批减仓',
      clientOrderId: 'batch-b',
      statusLabel: '已受理 · 待成交',
      executedQty: '0',
      error: '',
      terminal: false,
    },
    {
      id: 'repairs:0:repair-b',
      sideLabel: 'B · 只空',
      stageLabel: '恢复减仓',
      clientOrderId: 'repair-b',
      statusLabel: '部分成交',
      executedQty: '0.0050',
      error: '',
      terminal: false,
    },
  ]);
  assert.equal(diagnostic({}).stageLabel, '本批开仓');
  assert.equal(diagnostic({}, { phase: undefined }).stageLabel, '本批开仓');
});

test('old missing dispatch and invalid receipts never imply sending or a fill', () => {
  assert.equal(
    diagnostic({ receipt: { status: 'REJECTED', local_not_sent: true } })
      .statusLabel,
    '本地未发送',
  );
  for (const receipt of [undefined, null, false, 0, 'FILLED', [], {}]) {
    const legacy = diagnostic({ receipt });
    assert.equal(legacy.statusLabel, '尚无可核验回执');
    assert.equal(legacy.executedQty, '—');
    assert.equal(legacy.terminal, false);
    assert.equal(legacy.clientOrderId, '—');
    assert.equal(legacy.sideLabel, '未知侧');
    assert.equal(
      diagnostic({ receipt, dispatch: 'prepared' }).statusLabel,
      '尚未进入发送阶段',
    );
  }
  assert.equal(
    diagnostic({ dispatch: 'sending' }).statusLabel,
    '尚无可核验回执',
  );
  assert.equal(diagnostic({ receipt: { executedQty: '10' } }).terminal, false);
  assert.equal(
    diagnostic({ dispatch: 'prepared', receipt: { status: 'REJECTED' } })
      .statusLabel,
    '已拒绝',
  );
});

test('terminal display flags require an exact known terminal receipt status', () => {
  for (const status of [
    'FILLED',
    'CANCELED',
    'EXPIRED',
    'EXPIRED_IN_MATCH',
    'REJECTED',
  ]) {
    const result = diagnostic({ receipt: { status } });
    assert.equal(result.terminal, true, status);
    assert.ok(!result.statusLabel.includes('未知'));
  }
  for (const status of [
    'NEW',
    'PARTIALLY_FILLED',
    'PENDING_CANCEL',
    'UNKNOWN',
    'filled',
    ' FILLED ',
    'toString',
    '__proto__',
    '',
    ' ',
    null,
    { status: 'FILLED' },
    ['FILLED'],
  ]) {
    assert.equal(
      diagnostic({ receipt: { status, executedQty: '99' } }).terminal,
      false,
    );
  }
  assert.equal(
    diagnostic({ receipt: { status: 'UNKNOWN' } }).statusLabel,
    '未知状态 · UNKNOWN',
  );
});

test('quantities preserve zero and decimal precision without numeric coercion', () => {
  for (const executedQty of [
    '0',
    '0.0000',
    '001.2300',
    '0.0000000000000000001',
    '999999999999999999999999999999.0123456789',
  ]) {
    assert.equal(
      diagnostic({ receipt: { executedQty } }).executedQty,
      executedQty,
    );
  }
  for (const executedQty of [
    undefined,
    null,
    '',
    ' ',
    ' 1 ',
    '-1',
    '-0',
    '+1',
    '1e3',
    'NaN',
    'Infinity',
    '0x10',
    '1,000',
    '1.2.3',
    '1\n',
    0,
    1,
    Infinity,
    [],
    {},
  ]) {
    assert.equal(diagnostic({ receipt: { executedQty } }).executedQty, '—');
  }
});

test('malformed arrays and legs are ignored while valid empty legs keep a diagnostic', () => {
  for (const legs of [undefined, null, {}, 'invalid', 1]) {
    assert.deepEqual(
      pendingOrderDiagnostics(pair({ kind: 'ordinary', legs, repairs: legs })),
      [],
    );
  }
  const result = pendingOrderDiagnostics(
    pair({
      kind: 'ordinary',
      legs: [null, undefined, 'invalid', 0, [], {}],
      repairs: [{ key: 'other', order: [], receipt: [] }],
    }),
  );
  assert.equal(result.length, 2);
  assert.equal(result[0].id, 'legs:5:—');
  assert.equal(result[1].sideLabel, '未知侧');
});

test('identifiers stay stable and distinct across duplicate or missing client identifiers', () => {
  const fixture = pair({
    kind: 'ordinary',
    legs: [
      { order: { newClientOrderId: 'duplicate' } },
      { order: { newClientOrderId: 'duplicate' } },
      {},
    ],
    repairs: [{ order: { newClientOrderId: 'duplicate' } }, {}],
  });
  const result = pendingOrderDiagnostics(fixture);
  assert.equal(new Set(result.map(({ id }) => id)).size, 5);
  assert.deepEqual(pendingOrderDiagnostics(fixture), result);
});

test('error display selects only nonempty strings and limits them to 1000 characters', () => {
  assert.equal(
    diagnostic({
      error: 'query failed',
      receipt: { reject_reason: 'rejected' },
    }).error,
    'query failed',
  );
  for (const error of [undefined, null, '', '   ', {}, ['nested-secret'], 42]) {
    assert.equal(
      diagnostic({ error, receipt: { reject_reason: 'rejected' } }).error,
      'rejected',
    );
  }
  assert.equal(diagnostic({ error: 'x'.repeat(1200) }).error, 'x'.repeat(1000));
  assert.equal(
    diagnostic({ receipt: { reject_reason: 'y'.repeat(1200) } }).error,
    'y'.repeat(1000),
  );
});

test('code-like values remain plain strings while nested errors and raw receipts never leak', () => {
  const code = '<img src=x onerror="alert(1)">';
  const result = diagnostic({
    order: { newClientOrderId: code },
    error: code,
    receipt: { status: code, executedQty: code },
  });
  assert.equal(result.clientOrderId, code);
  assert.equal(result.error, code);
  assert.equal(result.statusLabel, `未知状态 · ${code}`);
  assert.equal(result.executedQty, '—');
  assert.equal(result.terminal, false);

  const nested = {
    token: 'nested-secret',
    toString() {
      throw new Error('must not coerce');
    },
  };
  const safe = diagnostic({
    key: nested,
    order: { newClientOrderId: nested, apiKey: 'order-secret' },
    error: nested,
    receipt: {
      status: nested,
      executedQty: nested,
      reject_reason: nested,
      signature: 'receipt-secret',
    },
  });
  assert.equal(safe.error, '');
  assert.equal(safe.clientOrderId, '—');
  assert.equal(safe.terminal, false);
  assert.doesNotMatch(JSON.stringify(safe), /secret|token|signature|apiKey/);
});

test('reading diagnostics never mutates pairs, order payloads, receipts or repair arrays', () => {
  const fixture = freeze(
    pair({
      kind: 'cycle',
      phase: 'open',
      legs: [
        {
          key: 'long',
          order: { newClientOrderId: 'original' },
          receipt: { status: 'FILLED', executedQty: '1.000' },
        },
      ],
      repairs: [
        {
          key: 'long',
          order: { newClientOrderId: 'repair' },
          receipt: null,
          error: 'still waiting',
        },
      ],
    }),
  );
  const original = structuredClone(fixture);
  assert.equal(hasPendingPairOrders(fixture), true);
  const result = pendingOrderDiagnostics(fixture);
  result[0].clientOrderId = 'display-only';
  result.reverse();
  assert.deepEqual(fixture, original);
});
