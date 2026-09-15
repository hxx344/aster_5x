import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  DEFAULT_CYCLE,
  cycleActualLeverage,
  cycleCountdown,
  cycleDraft,
  cycleHasPosition,
  cycleHoldSeconds,
  cycleSpreadView,
  cycleStatus,
  parseCycleDraft,
} from '../lib/cycle.ts';

test('cycle leverage comes only from a fresh matching exchange position pair', () => {
  const snapshot = {
    timestamp: 100,
    positions: [
      { symbol: 'XAUUSD1', side: 'LONG', leverage: 17 },
      { symbol: 'XAUUSD1', side: 'SHORT', leverage: 17 },
    ],
  };
  assert.equal(cycleActualLeverage(snapshot, 'XAUUSD1', 101), 17);
  assert.equal(cycleActualLeverage(snapshot, 'CLUSD1', 101), null);
  assert.equal(cycleActualLeverage(snapshot, 'XAUUSD1', 108), null);
  assert.equal(cycleActualLeverage(snapshot, 'XAUUSD1', 101, true), null);
  assert.equal(cycleActualLeverage(undefined, 'XAUUSD1', 101), null);
  snapshot.positions[1].leverage = 2;
  assert.equal(cycleActualLeverage(snapshot, 'XAUUSD1', 101), null);
});

test('requested 2x XAU, 10k depth, 0.1 bp and one minute settings retain their exact units', () => {
  const draft = cycleDraft();
  assert.equal(draft.hold_duration, '1');
  assert.equal(draft.hold_unit, 'minutes');
  assert.deepEqual(parseCycleDraft({ ...draft, enabled: true }), {
    ...DEFAULT_CYCLE,
    enabled: true,
  });
  assert.equal(
    parseCycleDraft({ ...draft, notional_scope: 'gross' }).notional_scope,
    'gross',
  );
});

test('hold conversion permits exact integer seconds without rounding the requested duration', () => {
  for (const [value, unit, result] of [
    ['1', 'minutes', 60],
    ['0.5', 'minutes', 30],
    ['0.05', 'minutes', 3],
    ['10080', 'minutes', 604800],
    ['604800', 'seconds', 604800],
    ['1.00', 'seconds', 1],
  ])
    assert.equal(cycleHoldSeconds(value, unit), result);
  for (const [value, unit] of [
    ['0', 'minutes'],
    ['0.01', 'minutes'],
    ['1.00000000000000001', 'seconds'],
    ['604800.000000000001', 'seconds'],
    ['10080.000000001', 'minutes'],
    ['1', 'hours'],
    ['-1', 'seconds'],
    ['NaN', 'minutes'],
    ['Infinity', 'seconds'],
    ['1e9', 'minutes'],
  ])
    assert.throws(() => cycleHoldSeconds(value, unit), /持仓时间|分钟或秒/);
  for (const seconds of [1, 59, 60, 61, 90, 3600, 604800])
    assert.equal(
      parseCycleDraft(cycleDraft({ ...DEFAULT_CYCLE, hold_seconds: seconds }))
        .hold_seconds,
      seconds,
    );
});

test('trading bounds are checked without losing significant decimal digits', () => {
  const draft = cycleDraft();
  const valid = parseCycleDraft({
    ...draft,
    leverage: '125',
    min_notional: '999999.999999999999999',
    max_notional: '1000000',
    spread_limit_bp: '100',
    spread_notional: '1000000',
  });
  assert.equal(valid.min_notional, '999999.999999999999999');
  for (const [key, value] of [
    ['leverage', '0'],
    ['leverage', '126'],
    ['leverage', '2.5'],
    ['leverage', 'NaN'],
    ['spread_limit_bp', '100.000000000000000001'],
    ['spread_limit_bp', '-0.1'],
    ['spread_notional', '0'],
    ['spread_notional', '1000000.000000000000001'],
    ['min_notional', '-1'],
    ['max_notional', '0'],
    ['max_notional', '1000000.00000000001'],
    ['symbol', 'BTCUSDT'],
    ['notional_scope', 'unknown'],
  ])
    assert.throws(
      () => parseCycleDraft({ ...draft, [key]: value }),
      undefined,
      `${key}: ${value}`,
    );
  assert.throws(
    () =>
      parseCycleDraft({
        ...draft,
        min_notional: '10000.000000000000001',
        max_notional: '10000',
      }),
    /下限不能大于上限/,
  );
  assert.equal(
    parseCycleDraft({ ...draft, spread_limit_bp: '0.0000000000000000001' })
      .spread_limit_bp,
    '0.0000000000000000001',
  );
});

test('capacity multiplier preserves decimals and rejects values below one or above 100', () => {
  const draft = cycleDraft();
  assert.equal(draft.capacity_multiplier, '1');
  assert.equal(
    parseCycleDraft({ ...draft, capacity_multiplier: '2.5' })
      .capacity_multiplier,
    '2.5',
  );
  for (const value of [
    '0',
    '0.99999999999999999999',
    '-1',
    '100.000000000000001',
    'NaN',
    'Infinity',
  ])
    assert.throws(
      () => parseCycleDraft({ ...draft, capacity_multiplier: value }),
      /额度倍数/,
    );
  const legacy = { ...DEFAULT_CYCLE };
  delete legacy.capacity_multiplier;
  assert.equal(cycleDraft(legacy).capacity_multiplier, '1');
});

test('missing or malformed numeric inputs never produce an executable configuration', () => {
  for (const key of [
    'spread_notional',
    'spread_limit_bp',
    'min_notional',
    'max_notional',
    'hold_duration',
  ]) {
    for (const value of [
      '',
      ' ',
      'NaN',
      'Infinity',
      '-Infinity',
      '1,000',
      '1e300',
      '9'.repeat(129),
    ])
      assert.throws(
        () => parseCycleDraft({ ...cycleDraft(), [key]: value }),
        undefined,
        `${key}: ${value}`,
      );
  }
});

test('cycle positions stay locked for either leg, including tiny residuals and invalid quantity records', () => {
  assert.equal(cycleHasPosition(), false);
  for (const quantity of ['0', '0.000', '-0', '0E-20'])
    assert.equal(
      cycleHasPosition({ quantities: { LONG: quantity, SHORT: '0' } }),
      false,
    );
  for (const quantity of [
    '1',
    '-1',
    '0.00000000001',
    '1E-999',
    'NaN',
    '',
    'unknown',
  ]) {
    assert.equal(
      cycleHasPosition({ quantities: { LONG: quantity, SHORT: '0' } }),
      true,
    );
    assert.equal(
      cycleHasPosition({ quantities: { LONG: '0', SHORT: quantity } }),
      true,
    );
  }
});

test('countdown is derived from confirmed server timestamps and never promises automatic closing', () => {
  const state = {
    quantities: { LONG: '1', SHORT: '1' },
    close_eligible_at: 1060,
  };
  assert.equal(cycleCountdown(state, 1000), '1 分 0 秒');
  assert.equal(cycleCountdown(state, 1059.1), '0 分 1 秒');
  assert.equal(cycleCountdown(state, 1060), '时间已满足，仍需价差达标');
  assert.equal(cycleCountdown(state, 1200), '时间已满足，仍需价差达标');
  assert.equal(cycleCountdown(state, 1200, true), '数据过期，等待刷新');
  assert.equal(
    cycleCountdown({ quantities: { LONG: '1' } }, 1000),
    '等待开仓确认',
  );
  assert.equal(cycleCountdown(undefined, 1000), '—');
});

test('status distinguishes modes without hiding server reconciliation reasons', () => {
  assert.equal(cycleStatus(undefined, false, true).phase, 'disabled');
  assert.equal(cycleStatus(undefined, true, false).phase, 'paused');
  assert.equal(cycleStatus(undefined, true, true).phase, 'waiting_open');
  const view = cycleStatus(
    { phase: 'reconciling', reason: '空头成交未确认' },
    true,
    false,
  );
  assert.equal(view.label, '核对中');
  assert.equal(view.reason, '空头成交未确认');
  assert.equal(
    cycleStatus({ phase: 'waiting_close' }, true, true).label,
    '等待平仓',
  );
});

test('drafts are independent of saved configurations and other accounts', () => {
  const first = cycleDraft({
    ...DEFAULT_CYCLE,
    symbol: 'CLUSD1',
    leverage: 3,
    hold_seconds: 90,
  });
  const second = cycleDraft();
  first.symbol = 'SPCXUSD1';
  first.spread_limit_bp = '0.2';
  assert.equal(second.symbol, 'XAUUSD1');
  assert.equal(second.spread_limit_bp, '0.1');
  assert.equal(DEFAULT_CYCLE.symbol, 'XAUUSD1');
});

test('depth spread ages from its own check time even while overall account state refreshes', () => {
  const state = { spread_bp: '0', spread_checked_at: 1000, updated_at: 1050 };
  assert.deepEqual(cycleSpreadView(state, 1003), {
    value: '0',
    timestamp: 1000,
    notice: '最近检测',
    stale: false,
  });
  assert.equal(cycleSpreadView(state, 1003.001).notice, '已过期');
  assert.equal(cycleSpreadView(state, 1001, true).notice, '已过期');
  assert.equal(cycleSpreadView(state, 998).stale, true);
  assert.equal(
    cycleSpreadView({ spread_bp: '0.01' }, 1001).notice,
    '检测时间未知',
  );
  assert.equal(
    cycleSpreadView({ spread_bp: '0.000000001', spread_checked_at: 1000 }, 1001)
      .value,
    '0.000000001',
  );
  for (const spread_bp of [undefined, null, '', 'NaN', 'Infinity', '-1']) {
    const result = cycleSpreadView(
      { spread_bp, spread_checked_at: 1000 },
      1001,
    );
    assert.equal(result.value, '—');
    assert.equal(result.notice, '等待检测');
  }
});
