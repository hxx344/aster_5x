import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  marginLimitFromPercent,
  migrationMarginLimit,
  parseMinimumLeverage,
  percentFromMarginLimit,
} from '../lib/policy.ts';

test('migration risk limits prefer the dedicated API value and support older responses', () => {
  assert.equal(
    migrationMarginLimit('0.93', {
      migration: '0.975',
      high_leverage: '0.98',
    }),
    '0.975',
  );
  assert.equal(migrationMarginLimit('0.93', { high_leverage: '0.98' }), '0.98');
  assert.equal(migrationMarginLimit('0.93'), '0.98');
  assert.equal(migrationMarginLimit('0.5', {}), '0.55');
});

test('migration fallback adds five percentage points exactly and caps at 100 percent', () => {
  for (const [base, expected] of [
    ['.93', '0.98'],
    ['9.3e-1', '0.98'],
    ['0.949999999999999999', '0.999999999999999999'],
    ['1e-18', '0.050000000000000001'],
    ['0.95', '1'],
    ['0.99', '1'],
    ['1', '1'],
  ])
    assert.equal(migrationMarginLimit(base), expected, base);
});

test('risk percentages convert and round-trip exactly', () => {
  for (const [percent, ratio] of [
    ['50', '0.5'],
    ['33.3', '0.333'],
    ['70', '0.7'],
    ['100', '1'],
    ['0.1', '0.001'],
    ['0.000000000000000001', '0.00000000000000000001'],
    ['99.99999999999999999', '0.9999999999999999999'],
  ]) {
    assert.equal(marginLimitFromPercent(percent), ratio);
    assert.equal(percentFromMarginLimit(ratio), percent);
  }
  assert.equal(marginLimitFromPercent('1e1'), '0.1');
  assert.equal(marginLimitFromPercent('050.00'), '0.5');
  assert.equal(marginLimitFromPercent('.1'), '0.001');
  assert.equal(percentFromMarginLimit('5E-1'), '50');
  assert.equal(
    marginLimitFromPercent(percentFromMarginLimit('1e-50')),
    `0.${'0'.repeat(49)}1`,
  );
});

test('invalid and out-of-range percentages never produce a policy', () => {
  for (const value of [
    '',
    ' ',
    '0',
    '-1',
    '101',
    '100.000000000000001',
    'NaN',
    'Infinity',
    'abc',
    '0x10',
    '1e200',
    '1e-101',
  ])
    assert.throws(() => marginLimitFromPercent(value), undefined, value);
});

test('minimum leverage accepts only supported opening tiers', () => {
  for (const value of [5, 10, 20])
    assert.equal(parseMinimumLeverage(String(value)), value);
  assert.equal(parseMinimumLeverage('5.0'), 5);
  assert.equal(parseMinimumLeverage('1e1'), 10);
  for (const value of [
    '',
    '0',
    '-1',
    '126',
    '5.5',
    '5.0000000000000001',
    '0x10',
    'NaN',
    'Infinity',
  ])
    assert.throws(() => parseMinimumLeverage(value), undefined, value);
  for (let value = 1; value <= 125; value++) {
    if ([5, 10, 20].includes(value)) continue;
    assert.throws(
      () => parseMinimumLeverage(String(value)),
      /只能选择 5x、10x 或 20x/,
      String(value),
    );
  }
});
