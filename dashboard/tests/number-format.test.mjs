import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  formatCount,
  formatNumber,
  formatPercent,
} from '../lib/number-format.ts';

test('reused formatters preserve missing, negative, rounding and precision behavior', () => {
  for (const digits of [0, 2, 4, 8]) {
    for (const value of [
      null,
      undefined,
      0,
      -0,
      -1234567.891,
      '0.00000123',
      '9999999.995',
    ]) {
      const expected =
        value == null
          ? '—'
          : Number(value).toLocaleString('en-US', {
              minimumFractionDigits: digits,
              maximumFractionDigits: digits,
            });
      assert.equal(formatNumber(value, digits), expected);
    }
  }
});

test('missing and invalid account values are not rendered as zero or a percentage', () => {
  for (const value of [
    undefined,
    null,
    '',
    ' ',
    'NaN',
    'Infinity',
    NaN,
    Infinity,
  ]) {
    assert.equal(formatNumber(value), '—');
    assert.equal(formatPercent(value), '—');
  }
  assert.equal(formatPercent('0'), '0.00%');
  assert.equal(formatPercent('0.5'), '50.00%');
  assert.equal(formatPercent(Number.MAX_VALUE), '—');
});

test('completed counts distinguish known zero from missing or invalid progress', () => {
  for (const value of [
    undefined,
    null,
    -1,
    0.5,
    NaN,
    Infinity,
    Number.MAX_SAFE_INTEGER + 1,
  ])
    assert.equal(formatCount(value), '—');
  assert.equal(formatCount(0), '0');
  assert.equal(formatCount(1234), '1,234');
});
