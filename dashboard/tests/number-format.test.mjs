import assert from 'node:assert/strict';
import { test } from 'node:test';
import { formatNumber } from '../lib/number-format.ts';

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
