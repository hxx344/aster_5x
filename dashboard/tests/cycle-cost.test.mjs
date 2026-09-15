import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  cycleAmount,
  cycleCostSummary,
  cycleDailySummary,
  cycleRollingSummary,
  cycleSignedAmount,
  cycleTradeCostView,
  cycleTradesForDate,
} from '../lib/cycle-daily.ts';

const cost = {
  taker_rate: '0.000125',
  taker_rate_percent: '0.0125',
  taker_fee: '5.000125',
  spread_cost: '-10.000000000000000001',
  total_cost: '-4.999875000000000001',
  unmatched_notional: '0',
  unmatched_fill_count: 0,
  complete: true,
};
const tradeCost = {
  taker_fee: '1.250125',
  spread_cost: '-1',
  total_cost: '0.250125',
  matched_quantity: '2',
  unmatched_quantity: '0',
  cost_complete: true,
};

test('signed cost amounts preserve negative values and decimal precision without changing volume formatting', () => {
  assert.equal(
    cycleSignedAmount('-1000000000000.00000000000001'),
    '-1,000,000,000,000.00000000000001',
  );
  assert.equal(
    cycleSignedAmount('-0.000000000000000001'),
    '-0.000000000000000001',
  );
  assert.equal(cycleSignedAmount('10000.000'), '10,000.000');
  assert.equal(cycleSignedAmount('0'), '0');
  assert.equal(cycleAmount('-1'), '—');
  for (const value of [
    undefined,
    null,
    '',
    'NaN',
    'Infinity',
    '-Infinity',
    '1e3',
    '--1',
    '- 1',
    0,
  ])
    assert.equal(cycleSignedAmount(value), '—');
});

test('complete paired cost displays the exact server fee, negative spread and signed total', () => {
  const view = cycleCostSummary(cost);
  assert.equal(view.fee, '5.000125');
  assert.equal(view.spread, '-10.000000000000000001');
  assert.equal(view.total, '-4.999875000000000001');
  assert.equal(view.complete, true);
  assert.equal(view.hasUnmatched, false);
  assert.equal(view.notice, '');
});

test('unmatched trades keep fee-only costs visibly partial and cannot be marked complete by a contradictory flag', () => {
  const partial = {
    ...cost,
    spread_cost: '0',
    total_cost: '5.000125',
    unmatched_notional: '40001',
    unmatched_fill_count: 2,
    complete: false,
  };
  const view = cycleCostSummary(partial);
  assert.equal(view.total, '5.000125');
  assert.equal(view.complete, false);
  assert.equal(view.unmatched, '40,001');
  assert.equal(view.unmatchedCount, '2');
  assert.match(view.notice, /待配对/);
  assert.equal(
    cycleCostSummary({ ...partial, complete: true }).complete,
    false,
  );
});

test('missing or failed cost reports never become a complete zero-cost report', () => {
  const absent = cycleCostSummary(undefined);
  assert.equal(absent.total, '—');
  assert.equal(absent.fee, '—');
  assert.equal(absent.complete, false);
  assert.match(absent.notice, /尚未提供/);
  assert.equal(cycleCostSummary(undefined, true).staleNotice, '');
  const failed = cycleCostSummary({
    ...cost,
    taker_fee: null,
    spread_cost: null,
    total_cost: null,
    unmatched_notional: null,
    unmatched_fill_count: null,
    complete: false,
    error: '成本统计暂不可用',
  });
  assert.equal(failed.total, '—');
  assert.equal(failed.unmatched, '—');
  assert.equal(failed.unmatchedCount, '—');
  assert.match(failed.notice, /暂不可用/);
  assert.equal(
    cycleCostSummary({ ...cost, total_cost: undefined }).complete,
    false,
  );
  assert.equal(
    cycleCostSummary({ ...cost, unmatched_fill_count: undefined }).complete,
    false,
  );
  assert.equal(
    cycleCostSummary({ ...cost, unmatched_notional: undefined }).complete,
    false,
  );
  assert.equal(
    cycleCostSummary({ ...cost, complete: undefined }).complete,
    false,
  );
});

test('pending reconciliation and stale windows preserve recorded amounts with explicit notices', () => {
  const pending = cycleCostSummary({ ...cost, sync_pending: true }, true);
  assert.equal(pending.total, '-4.999875000000000001');
  assert.equal(pending.complete, false);
  assert.match(pending.notice, /补账/);
  assert.match(pending.staleNotice, /最近统计/);
  const stale = cycleCostSummary(cost, true);
  assert.equal(stale.fee, '5.000125');
  assert.match(stale.staleNotice, /待刷新/);
});

test('individual fills distinguish fee-only pending pairs, completed pairs and missing cost details', () => {
  const first = cycleTradeCostView({
    ...tradeCost,
    spread_cost: '0',
    total_cost: '1.250125',
    matched_quantity: '0',
    unmatched_quantity: '2',
    cost_complete: false,
  });
  assert.equal(first.total, '1.250125');
  assert.match(first.notice, /待配对数量 2/);
  assert.equal(first.complete, false);
  const later = cycleTradeCostView(tradeCost);
  assert.equal(later.total, '0.250125');
  assert.equal(later.spread, '-1');
  assert.equal(later.complete, true);
  assert.equal(later.notice, '');
  const missing = cycleTradeCostView(undefined);
  assert.equal(missing.total, '—');
  assert.match(missing.notice, /未提供/);
  assert.equal(
    cycleTradeCostView({ ...tradeCost, matched_quantity: undefined }).complete,
    false,
  );
  assert.equal(
    cycleTradeCostView({ ...tradeCost, total_cost: null }).complete,
    false,
  );
});

test('UTC day costs and rolling costs stay separate across midnight and filters do not recompute attribution', () => {
  const midnight = Date.parse('2026-09-14T00:00:00Z') / 1000;
  const daily = {
    utc_date: '2026-09-13',
    volume: '10000',
    trade_count: 1,
    limit: '40000',
    remaining: '30000',
    next_reset_at: midnight,
    cost: { ...cost, taker_fee: '1.25', spread_cost: '0', total_cost: '1.25' },
  };
  const rolling = {
    window_start: midnight - 86400,
    window_end: midnight,
    volume: '20001',
    trade_count: 2,
    limit: '40000',
    remaining: '19999',
    next_release_at: midnight + 86399,
    cost: {
      ...cost,
      taker_fee: '2.500125',
      spread_cost: '-1',
      total_cost: '1.500125',
    },
  };
  const dayView = cycleDailySummary(daily, midnight);
  assert.equal(dayView.rolloverPending, true);
  assert.equal(
    cycleCostSummary(daily.cost, dayView.rolloverPending).total,
    '1.25',
  );
  assert.equal(cycleRollingSummary(rolling, midnight).stale, false);
  assert.equal(cycleCostSummary(rolling.cost).total, '1.500125');
  const trades = [
    {
      utc_date: '2026-09-13',
      cost: {
        ...tradeCost,
        taker_fee: '1.25',
        spread_cost: '0',
        total_cost: '1.25',
      },
    },
    { utc_date: '2026-09-14', cost: tradeCost },
  ];
  const filtered = cycleTradesForDate(trades, '2026-09-14');
  assert.equal(filtered.length, 1);
  assert.equal(cycleTradeCostView(filtered[0].cost).spread, '-1');
  assert.equal(cycleTradeCostView(trades[0].cost).spread, '0');
});
