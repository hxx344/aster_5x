import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  marginLimitFromPercent,
  parseMinimumLeverage,
  percentFromMarginLimit,
} from '../lib/policy.ts';

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

test('minimum leverage accepts integer bounds and custom tiers only', () => {
  for (const value of [1, 2, 4, 7, 10, 125])
    assert.equal(parseMinimumLeverage(String(value)), value);
  assert.equal(parseMinimumLeverage('7.0'), 7);
  assert.equal(parseMinimumLeverage('1e1'), 10);
  for (const value of [
    '',
    '0',
    '-1',
    '126',
    '4.5',
    '4.0000000000000001',
    '0x10',
    'NaN',
    'Infinity',
  ])
    assert.throws(() => parseMinimumLeverage(value), undefined, value);
});
