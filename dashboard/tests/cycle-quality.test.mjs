import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  cycleExecutionQualityView,
  cycleQualityTime,
} from '../lib/cycle-quality.ts';

const started = Date.parse('2026-09-14T23:59:59.900Z') / 1000;
const estimate = {
  status: 'available',
  sampled_at: started - 0.1,
  checked_at: started - 0.01,
  quantity: '2.500',
  buy_vwap: '3624.12345678901234567890123456789012345',
  sell_vwap: '3624.1',
  spread_bp: '0.0647226655443322110099887766554433221',
};
const quality = {
  version: 1,
  intent_id: 'batch-a',
  symbol: 'XAUUSD1',
  phase: 'open',
  quantity: '2.5',
  created_at: started,
  updated_at: started + 0.2,
  trigger: { source: 'depth', received_at: started - 0.05 },
  trigger_estimate: estimate,
  final_estimate: { ...estimate, spread_bp: '0.01' },
  timing: {
    request_started_at: started,
    response_received_at: started + 0.1,
    trigger_to_request_ms: 50.001,
    final_check_to_request_ms: 10.002,
    request_to_response_ms: 100.003,
    request_status: 'returned',
  },
  actual: {
    status: 'filled',
    confirmed: true,
    quantity: '2.5000',
    buy_quantity: '2.5',
    sell_quantity: '02.500',
    buy_vwap: '3624.0',
    sell_vwap: '3624.01',
    spread_bp: '-0.027594885890654676787',
    buy_status: 'FILLED',
    sell_status: 'FILLED',
    repairs_present: false,
  },
};

test('same-quantity comparison preserves server precision and favorable negative actual spread', () => {
  const view = cycleExecutionQualityView(quality);
  assert.equal(view.quantity, '2.5');
  assert.equal(view.trigger.available, true);
  assert.equal(view.trigger.spread, estimate.spread_bp);
  assert.equal(view.trigger.buy, estimate.buy_vwap);
  assert.equal(view.final.spread, '0.01');
  assert.equal(view.actual.complete, true);
  assert.equal(view.actual.spread, quality.actual.spread_bp);
  assert.deepEqual(
    view.timing.map((item) => item.value),
    ['50.001', '10.002', '100.003'],
  );
  assert.equal(view.updatedAt, '2026-09-15 00:00:00.100 UTC');
});

test('partial, pending, rejected, unsent and unknown results cannot become complete from populated price fields', () => {
  for (const status of [
    'partial',
    'pending',
    'rejected',
    'not_sent',
    'unknown',
    undefined,
    'unexpected',
    'constructor',
  ]) {
    const view = cycleExecutionQualityView({
      ...quality,
      actual: { ...quality.actual, status },
    });
    assert.equal(view.actual.complete, false);
    assert.equal(view.actual.spread, '—');
    assert.equal(view.actual.buy, '—');
    assert.equal(view.actual.sell, '—');
    assert.equal(view.actual.quantity, '—');
    assert.notEqual(view.actual.status, '双边已确认');
    assert.equal(view.actual.buyQuantity, quality.actual.buy_quantity);
  }
});

test('actual comparison requires strict confirmation and both original orders filled at exactly the batch quantity', () => {
  for (const patch of [
    { confirmed: false },
    { confirmed: 'true' },
    { confirmed: null },
    { buy_status: 'PARTIALLY_FILLED' },
    { sell_status: null },
    { quantity: null },
    { buy_quantity: '2.49999999999999999999999999999999999' },
    { sell_quantity: '2.50000000000000000000000000000000001' },
    { buy_vwap: '0' },
    { sell_vwap: '-1' },
    { spread_bp: null },
  ]) {
    const view = cycleExecutionQualityView({
      ...quality,
      actual: { ...quality.actual, ...patch },
    });
    assert.equal(view.actual.complete, false);
    assert.equal(view.actual.spread, '—');
  }
});

test('unavailable, malformed or differently sized estimates cannot be compared as the same batch', () => {
  for (const source of [
    null,
    undefined,
    { ...estimate, status: 'unavailable' },
    { ...estimate, status: true },
    { ...estimate, quantity: '2.50000000000000000000000000000000001' },
    { ...estimate, quantity: '0' },
    { ...estimate, quantity: null },
    { ...estimate, buy_vwap: null },
    { ...estimate, sell_vwap: Infinity },
    { ...estimate, spread_bp: 'NaN' },
    { ...estimate, spread_bp: 0 },
  ]) {
    const view = cycleExecutionQualityView({
      ...quality,
      trigger_estimate: source,
      final_estimate: source,
    });
    assert.equal(view.trigger.available, false);
    assert.equal(view.final.available, false);
    assert.equal(view.trigger.spread, '—');
    assert.equal(view.final.spread, '—');
  }
});

test('genuine zero spread and zero measured duration survive without converting missing values to zero', () => {
  const view = cycleExecutionQualityView({
    ...quality,
    trigger_estimate: { ...estimate, spread_bp: '0.000' },
    actual: { ...quality.actual, spread_bp: '-0.000' },
    timing: {
      ...quality.timing,
      trigger_to_request_ms: 0,
      final_check_to_request_ms: 0,
      request_to_response_ms: 0,
    },
  });
  assert.equal(view.trigger.spread, '0.000');
  assert.equal(view.actual.spread, '-0.000');
  assert.deepEqual(
    view.timing.map((item) => item.value),
    ['0', '0', '0'],
  );
  for (const invalid of [null, undefined, '', '0', false, -1, NaN, Infinity]) {
    const missing = cycleExecutionQualityView({
      ...quality,
      timing: {
        ...quality.timing,
        trigger_to_request_ms: invalid,
        final_check_to_request_ms: invalid,
        request_to_response_ms: invalid,
      },
    });
    assert.deepEqual(
      missing.timing.map((item) => item.value),
      ['—', '—', '—'],
    );
  }
});

test('failed or unconfirmed submit cannot claim a received response or a round-trip duration', () => {
  for (const request_status of ['failed', 'not_sent', 'unknown', undefined]) {
    const view = cycleExecutionQualityView({
      ...quality,
      timing: { ...quality.timing, request_status },
    });
    assert.equal(view.timing[2].value, '—');
    assert.equal(view.responseReceivedAt, '—');
    assert.notEqual(view.requestStatus, '调用已返回');
  }
  for (const patch of [
    { request_started_at: null },
    { response_received_at: null },
    { response_received_at: started - 1 },
    { request_started_at: Infinity },
  ]) {
    const view = cycleExecutionQualityView({
      ...quality,
      timing: { ...quality.timing, ...patch },
    });
    assert.equal(view.timing[2].value, '—');
  }
});

test('poll wakeups and unknown triggers never appear as measured WebSocket latency', () => {
  const poll = cycleExecutionQualityView({
    ...quality,
    trigger: { ...quality.trigger, source: 'poll' },
  });
  assert.equal(poll.timing[0].label, '轮询唤醒 → 请求开始');
  assert.equal(poll.triggerSource, '轮询唤醒');
  const unknown = cycleExecutionQualityView({
    ...quality,
    trigger: { ...quality.trigger, source: 'unknown' },
  });
  assert.equal(unknown.timing[0].value, '—');
  for (const received_at of [null, undefined, started + 1, '1789430399.8']) {
    const view = cycleExecutionQualityView({
      ...quality,
      trigger: { source: 'bbo', received_at },
    });
    assert.equal(view.timing[0].value, '—');
  }
});

test('record timestamps remain historical with milliseconds and invalid wall times stay unknown', () => {
  assert.equal(cycleQualityTime(started), '2026-09-14 23:59:59.900 UTC');
  for (const invalid of [
    null,
    undefined,
    '1789430399.9',
    -1,
    NaN,
    Infinity,
    9e15,
  ])
    assert.equal(cycleQualityTime(invalid), '—');
  const old = cycleExecutionQualityView({ ...quality, updated_at: 0 });
  assert.equal(old.updatedAt, '1970-01-01 00:00:00.000 UTC');
});

test('repairs remain separate while confirmed original pair measurements stay inspectable', () => {
  const view = cycleExecutionQualityView({
    ...quality,
    actual: { ...quality.actual, repairs_present: true },
  });
  assert.equal(view.actual.repairs, true);
  assert.equal(view.actual.complete, true);
  assert.equal(view.actual.spread, quality.actual.spread_bp);
});

test('old responses, incomplete new responses and account changes never retain another accounts result', () => {
  const original = structuredClone(quality);
  assert.equal(cycleExecutionQualityView(undefined), null);
  assert.equal(cycleExecutionQualityView(null), null);
  assert.equal(cycleExecutionQualityView({ ...quality, version: 2 }), null);
  const incomplete = cycleExecutionQualityView({ version: 1 });
  assert.equal(incomplete.quantity, '—');
  assert.equal(incomplete.actual.spread, '—');
  assert.deepEqual(
    incomplete.timing.map((item) => item.value),
    ['—', '—', '—'],
  );
  const a = cycleExecutionQualityView(quality);
  const b = cycleExecutionQualityView({
    ...quality,
    symbol: 'CLUSD1',
    phase: 'close',
    intent_id: 'batch-b',
    actual: null,
  });
  assert.equal(a.symbol, 'XAUUSD1');
  assert.equal(b.symbol, 'CLUSD1');
  assert.equal(b.phase, '平仓');
  assert.equal(b.actual.spread, '—');
  assert.equal(cycleExecutionQualityView(undefined), null);
  assert.deepEqual(quality, original);
});

test('pre-submit measurements remain separate from the existing overlapping total durations', () => {
  const source = {
    ...quality,
    timing: {
      ...quality.timing,
      pre_submit: {
        queue_ms: 3.125,
        initial_account_ms: 150.01,
        planning_ms: 0,
        final_account_ms: 200.02,
        final_check_ms: 17.3,
        persist_ms: 2.1,
        other_ms: 36.445,
      },
    },
  };
  const original = structuredClone(source);
  const view = cycleExecutionQualityView(source);
  assert.deepEqual(
    view.preSubmit.map((item) => item.label),
    [
      '触发→账户任务开始',
      '首轮账户查询',
      '首轮规划与盘口',
      '提交前账户复核',
      '最终盘口与风控检查',
      '订单记录写入',
      '其他本地准备',
    ],
  );
  assert.deepEqual(
    view.preSubmit.map((item) => item.value),
    ['3.125', '150.01', '0', '200.02', '17.3', '2.1', '36.445'],
  );
  assert.deepEqual(view.timing, cycleExecutionQualityView(quality).timing);
  assert.deepEqual(source, original);
});

test('old batches and missing pre-submit fields remain unknown instead of inheriting a total or HTTP duration', () => {
  for (const pre_submit of [undefined, null, {}, { queue_ms: 0 }]) {
    const view = cycleExecutionQualityView({
      ...quality,
      timing: { ...quality.timing, pre_submit },
    });
    assert.equal(view.preSubmit.length, 7);
    assert.deepEqual(
      view.preSubmit.map((item) => item.value),
      [pre_submit?.queue_ms === 0 ? '0' : '—', '—', '—', '—', '—', '—', '—'],
    );
    assert.equal(view.timing[2].value, '100.003');
  }
});

test('pre-submit fields reject negative, nonfinite and nonnumeric durations independently', () => {
  const keys = [
    'queue_ms',
    'initial_account_ms',
    'planning_ms',
    'final_account_ms',
    'final_check_ms',
    'persist_ms',
    'other_ms',
  ];
  for (const invalid of [
    -1,
    NaN,
    Infinity,
    -Infinity,
    null,
    undefined,
    false,
    '',
    '0',
    '17.3',
  ]) {
    const pre_submit = Object.fromEntries(keys.map((key) => [key, invalid]));
    const view = cycleExecutionQualityView({
      ...quality,
      timing: { ...quality.timing, pre_submit },
    });
    assert.deepEqual(
      view.preSubmit.map((item) => item.value),
      keys.map(() => '—'),
    );
  }
  const partial = cycleExecutionQualityView({
    ...quality,
    timing: {
      ...quality.timing,
      pre_submit: { queue_ms: 4.75, planning_ms: -1, other_ms: 0 },
    },
  });
  assert.deepEqual(
    partial.preSubmit.map((item) => item.value),
    ['4.75', '—', '—', '—', '—', '—', '0'],
  );
});
