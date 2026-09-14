import assert from 'node:assert/strict';
import { test } from 'node:test';
import { ordinaryConditionsView } from '../lib/ordinary-conditions.ts';

const now = 1000;
function fixture() {
  return {
    account: {
      policy: {
        threshold: '10000',
        margin_limit: '0.5',
        min_open_leverage: 5,
        spread_limit: '0.0005',
      },
      risk_limits: { high_leverage: '0.55' },
      snapshot: {
        timestamp: now,
        ratio: '0.52',
        positions: [
          { symbol: 'XAUUSD1', side: 'LONG', leverage: 2 },
          { symbol: 'XAUUSD1', side: 'SHORT', leverage: 2 },
        ],
      },
      cycle: { enabled: true, symbol: 'XAUUSD1' },
      ordinary_add_blocks: { XAUUSD1: '循环占用，普通加仓禁用' },
    },
    market: {
      status: 'ok',
      checked_at: now,
      capacities: { 5: '10001', 10: '10000', 20: '9999' },
      book: { spread: '0.0005', timestamp: now },
    },
  };
}
const view = ({ account, market }, time = now, error = '') =>
  ordinaryConditionsView(account, market, 'XAUUSD1', time, error);

test('cycle 2x leaves all three ordinary conditions visible without turning restrictions into condition failures', () => {
  const result = view(fixture());
  assert.equal(result.currentLeverage, 2);
  assert.equal(result.leverageConstraint, 'unsupported');
  assert.deepEqual(
    result.tiers.map((tier) => [
      tier.leverage,
      tier.current,
      tier.capacity.state,
      tier.margin.state,
    ]),
    [
      [5, false, 'met', 'unmet'],
      [10, false, 'unmet', 'met'],
      [20, false, 'unmet', 'met'],
    ],
  );
  assert.deepEqual(
    result.tiers.map((tier) => tier.margin.required),
    ['0.5', '0.55', '0.55'],
  );
  assert.equal(result.spread.state, 'met');
});

test('each condition expires only from its own backend timestamp at the backend boundary', () => {
  const source = fixture();
  source.market.checked_at = now - 8;
  source.account.snapshot.timestamp = now - 8;
  source.market.book.timestamp = now - 3;
  assert.equal(view(source).tiers[0].capacity.state, 'met');
  assert.equal(view(source).tiers[1].margin.state, 'met');
  assert.equal(view(source).spread.state, 'met');
  assert.equal(view(source, now + 0.01).spread.state, 'stale');
  assert.equal(view(source, now + 0.01).tiers[0].capacity.state, 'stale');
  assert.equal(view(source, now + 0.01).tiers[1].margin.state, 'stale');
  source.market.checked_at = now;
  source.account.snapshot.timestamp = now;
  assert.equal(view(source, now + 0.01).tiers[0].capacity.state, 'met');
  assert.equal(view(source, now + 0.01).spread.state, 'stale');
  assert.equal(view(source, now + 0.01).spread.actual, '0.0005');
});

test('missing, failed and future data cannot render satisfied conditions or invented zero values', () => {
  const source = fixture();
  source.account.snapshot.ratio = null;
  delete source.market.capacities[5];
  delete source.market.book.timestamp;
  let result = view(source);
  assert.equal(result.tiers[0].capacity.actual, null);
  assert.equal(result.tiers[0].capacity.state, 'unknown');
  assert.equal(result.tiers[0].margin.actual, null);
  assert.equal(result.tiers[0].margin.state, 'unknown');
  assert.equal(result.spread.state, 'unknown');
  source.market.book.timestamp = now + 2;
  assert.equal(view(source).spread.state, 'unknown');
  source.market.book.timestamp = now;
  source.market.book_error = 'BBO 获取失败';
  assert.equal(view(source).spread.state, 'unknown');
  delete source.market.book_error;
  source.market.status = 'error';
  result = view(source);
  assert.equal(result.tiers[1].capacity.state, 'unknown');
  assert.equal(result.spread.state, 'met');
  assert.equal(view(source, now, '连接异常').spread.state, 'stale');
  assert.equal(
    ordinaryConditionsView(undefined, undefined, 'XAUUSD1', now).tiers[0].margin
      .state,
    'unknown',
  );
});

test('real 5x, 10x and 20x tiers need fresh matching long and short records, never a minimum-leverage fallback', () => {
  const source = fixture();
  for (const leverage of [5, 10, 20]) {
    source.account.snapshot.positions.forEach((position) => {
      position.leverage = leverage;
    });
    const result = view(source);
    assert.equal(result.currentLeverage, leverage);
    assert.equal(result.leverageConstraint, null);
    assert.deepEqual(
      result.tiers.filter((tier) => tier.current).map((tier) => tier.leverage),
      [leverage],
    );
  }
  source.account.policy.min_open_leverage = 20;
  source.account.snapshot.positions.forEach((position) => {
    position.leverage = 10;
  });
  assert.equal(view(source).leverageConstraint, 'below_minimum');
  source.account.snapshot.positions = [];
  assert.equal(view(source).currentLeverage, null);
  assert.deepEqual(
    view(source).tiers.map((tier) => tier.belowMinimum),
    [true, true, false],
  );
  source.account.snapshot.positions = [
    { symbol: 'CLUSD1', side: 'LONG', leverage: 5 },
    { symbol: 'CLUSD1', side: 'SHORT', leverage: 5 },
  ];
  assert.equal(view(source).currentLeverage, null);
  source.account.snapshot.positions = [
    { symbol: 'XAUUSD1', side: 'LONG', leverage: 10 },
    { symbol: 'XAUUSD1', side: 'SHORT', leverage: 20 },
  ];
  assert.equal(view(source).currentLeverage, null);
  source.account.snapshot.positions[1].leverage = 10;
  source.account.snapshot.timestamp = now - 9;
  assert.equal(view(source).currentLeverage, null);
});

test('strict quota and margin boundaries preserve decimal precision and policy spread limits', () => {
  const source = fixture();
  source.market.capacities[5] = '10000.000000000000000001';
  source.account.snapshot.ratio = '0.49999999999999999999';
  source.account.policy.spread_limit = '0.0001';
  let result = view(source);
  assert.equal(result.tiers[0].capacity.state, 'met');
  assert.equal(result.tiers[0].margin.state, 'met');
  assert.equal(result.spread.state, 'unmet');
  assert.equal(result.spread.required, '0.0001');
  source.account.snapshot.ratio = '0.5';
  assert.equal(view(source).tiers[0].margin.state, 'unmet');
  source.account.snapshot.ratio = 'NaN';
  source.market.capacities[5] = '';
  result = view(source);
  assert.equal(result.tiers[0].margin.state, 'unknown');
  assert.equal(result.tiers[0].capacity.state, 'unknown');
});
