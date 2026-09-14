import assert from 'node:assert/strict';
import { test } from 'node:test';
import { cycleMarginLimit, percentFromMarginLimit } from '../lib/policy.ts';

test('cycle margin display uses only the dedicated effective account-wide limit', () => {
  const reported = cycleMarginLimit({
    cycle: '0.55',
    base: '0.5',
    high_leverage: '0.5',
  });
  assert.equal(reported, '0.55');
  assert.equal(percentFromMarginLimit(reported), '55');
  assert.equal(cycleMarginLimit({ cycle: '1' }), '1');
  assert.equal(
    cycleMarginLimit({ cycle: '0.999999999999999999' }),
    '0.999999999999999999',
  );
});

test('older responses never claim the extra allowance is active without a cycle field', () => {
  assert.equal(cycleMarginLimit(undefined), null);
  assert.equal(cycleMarginLimit({}), null);
  assert.equal(
    cycleMarginLimit({ base: '0.5', high_leverage: '0.55', migration: '0.55' }),
    null,
  );
});

test('invalid, zero or excessive reported cycle limits stay unknown', () => {
  for (const value of [
    null,
    0.55,
    '',
    '0',
    '-0.05',
    '1.000000000000000001',
    'NaN',
    'Infinity',
  ])
    assert.equal(cycleMarginLimit({ cycle: value }), null);
});
