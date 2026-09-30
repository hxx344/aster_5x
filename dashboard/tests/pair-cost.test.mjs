import assert from 'node:assert/strict';
import { test } from 'node:test';
import { pairCostFresh, pairCostPeriodView } from '../lib/pair-cost.ts';

const monday = Date.UTC(2026, 8, 28) / 1000;
const period = (changes = {}) => ({
  start: monday,
  end: monday + 86400,
  label: '2026-09-28',
  estimated_fee: '0.250125',
  spread_cost: '1',
  slippage_cost: '-0.125',
  total_cost: '1.250125',
  complete: true,
  slippage_complete: true,
  fill_count: 2,
  unmatched_notional: '0',
  missing_count: 0,
  unassigned_count: 0,
  ...changes,
});
const report = () => ({
  as_of: monday + 100,
  timezone: 'UTC',
  fee_rate_percent: '0.0125',
  daily: period(),
  weekly: period({ end: monday + 7 * 86400 }),
});
const feeScenario = (changes = {}) => ({
  basis: 'assumed_aster5_referral10_after_discount',
  aster_discount: '0.01250625',
  discounted_fee: '0.23761875',
  rebate: '0.023761875',
  final_fee: '0.213856875',
  total_cost: '1.213856875',
  ...changes,
});

test('conditional fee scenario formats server amounts without losing precision', () => {
  const view = pairCostPeriodView(
    period({
      fee_scenario: feeScenario({
        final_fee: '9007199254740993.00000000000001',
        total_cost: '-0.99999999999999',
      }),
    }),
  );
  assert.equal(view.scenarioAvailable, true);
  assert.equal(view.scenario.discount, '0.01250625');
  assert.equal(view.scenario.discountedFee, '0.23761875');
  assert.equal(view.scenario.rebate, '0.023761875');
  assert.equal(view.scenario.finalFee, '9,007,199,254,740,993.00000000000001');
  assert.equal(view.scenario.total, '-0.99999999999999');
  assert.equal(view.fee, '0.250125');
  assert.equal(view.total, '1.250125');
});

test('old responses retain gross totals without inventing discounted fees', () => {
  const view = pairCostPeriodView(period());
  assert.equal(view.scenarioAvailable, false);
  assert.equal(view.scenario.finalFee, '—');
  assert.equal(view.scenario.total, '—');
  assert.equal(view.total, '1.250125');
  assert.equal(view.complete, true);
  assert.match(view.notices.join(''), /优惠前口径/);
});

test('unknown fee basis or invalid adjustment amounts cannot appear as valid savings', () => {
  const invalid = [
    null,
    { basis: 'unknown' },
    ...[
      'aster_discount',
      'discounted_fee',
      'rebate',
      'final_fee',
      'total_cost',
    ].map((key) => feeScenario({ [key]: 'NaN' })),
    feeScenario({ rebate: '-1' }),
  ];
  for (const value of invalid) {
    const view = pairCostPeriodView(period({ fee_scenario: value }));
    assert.equal(view.scenarioAvailable, false);
    assert.equal(view.scenario.finalFee, '—');
    assert.equal(view.total, '1.250125');
    assert.match(view.notices.join(''), /优惠估算无效/);
  }
});

test('fee scenario preserves incomplete receipt status and confirmed zero costs', () => {
  const partial = pairCostPeriodView(
    period({
      fee_scenario: feeScenario(),
      complete: false,
      missing_count: 1,
    }),
  );
  assert.equal(partial.scenarioAvailable, true);
  assert.equal(partial.complete, false);
  const zero = pairCostPeriodView(
    period({
      fee_scenario: feeScenario({
        aster_discount: '0',
        discounted_fee: '0',
        rebate: '0',
        final_fee: '0',
        total_cost: '0',
      }),
      estimated_fee: '0',
      spread_cost: '0',
      total_cost: '0',
      fill_count: 0,
    }),
  );
  assert.equal(zero.scenario.finalFee, '0');
  assert.equal(zero.scenarioAvailable, true);
  assert.equal(zero.complete, true);
});

test('costs preserve exact decimals, signed improvements and server totals', () => {
  const view = pairCostPeriodView(
    period({
      estimated_fee: '9007199254740993.00000000000001',
      spread_cost: '-9007199254740994',
      total_cost: '-0.99999999999999',
    }),
  );
  assert.equal(view.fee, '9,007,199,254,740,993.00000000000001');
  assert.equal(view.spread, '-9,007,199,254,740,994');
  assert.equal(view.total, '-0.99999999999999');
  assert.equal(view.slippage, '-0.125');
  assert.equal(view.complete, true);
  assert.equal(view.slippageComplete, true);
});

test('missing report is unknown while confirmed no trades is zero', () => {
  const missing = pairCostPeriodView();
  assert.equal(missing.total, '—');
  assert.equal(missing.complete, false);
  assert.match(missing.notices.join(''), /等待/);
  const empty = pairCostPeriodView(
    period({
      estimated_fee: '0',
      spread_cost: '0',
      total_cost: '0',
      slippage_cost: '0',
      fill_count: 0,
    }),
  );
  assert.equal(empty.total, '0');
  assert.equal(empty.complete, true);
  assert.equal(empty.slippage, '0');
});

test('partial fees, uncertain time and unmatched exposure cannot look complete', () => {
  const view = pairCostPeriodView(
    period({
      unmatched_notional: '1000.000001',
      missing_count: 1,
      unassigned_count: 2,
    }),
  );
  assert.equal(view.total, '1.250125');
  assert.equal(view.complete, false);
  assert.match(view.notices.join(''), /1 项/);
  assert.match(view.notices.join(''), /2 项/);
  assert.match(view.notices.join(''), /1,000.000001 USD1/);
});

test('missing slippage basis is independent of known total cost', () => {
  const missing = pairCostPeriodView(
    period({ slippage_cost: null, slippage_complete: false }),
  );
  assert.equal(missing.slippage, '—');
  assert.equal(missing.slippageComplete, false);
  assert.equal(missing.complete, true);
  const partial = pairCostPeriodView(
    period({ slippage_cost: '0.01', slippage_complete: false }),
  );
  assert.equal(partial.slippage, '0.01');
  assert.equal(partial.slippageComplete, false);
});

test('freshness expires on disconnect, age, clock reversal and UTC window boundary', () => {
  const value = report();
  assert.equal(pairCostFresh(value, monday + 105, false), true);
  assert.equal(pairCostFresh(value, monday + 105, true), false);
  assert.equal(pairCostFresh(value, monday + 130, false), false);
  assert.equal(pairCostFresh(value, monday + 99.5, false), true);
  assert.equal(pairCostFresh(value, monday + 98, false), false);
  assert.equal(pairCostFresh(null, monday + 105, false), false);
  value.as_of = monday + 86400 - 1;
  assert.equal(pairCostFresh(value, monday + 86400, false), false);
  value.daily = period({ start: monday + 7 * 86400, end: monday + 8 * 86400 });
  value.as_of = monday + 7 * 86400;
  assert.equal(pairCostFresh(value, value.as_of, false), false);
});

test('UTC periods use exclusive end and invalid values cannot claim completeness', () => {
  assert.equal(pairCostPeriodView(period()).range, '2026-09-28');
  assert.equal(
    pairCostPeriodView(report().weekly).range,
    '2026-09-28 — 2026-10-04',
  );
  for (const changes of [
    { estimated_fee: 'NaN' },
    { total_cost: undefined },
    { fill_count: -1 },
    { unmatched_notional: null },
  ]) {
    assert.equal(pairCostPeriodView(period(changes)).complete, false);
  }
});
