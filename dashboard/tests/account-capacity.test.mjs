import assert from 'node:assert/strict';
import { test } from 'node:test';
import { accountCapacityView } from '../lib/account-capacity.ts';

const room = {
  leverage: 5,
  cap: '1000',
  occupied: '1000',
  remaining: '0',
  checked_at: 100,
  expires_at: 105,
};

test('zero remains valid; stale, missing, disconnected and wrong-leverage data stay unavailable', () => {
  assert.equal(accountCapacityView(room, 5, 104, false).ready, true);
  assert.equal(accountCapacityView(room, 5, 105, false).ready, false);
  assert.equal(accountCapacityView(room, 10, 101, false).ready, false);
  assert.equal(accountCapacityView(undefined, 5, 101, false).ready, false);
  assert.equal(accountCapacityView(room, 5, 101, true).ready, false);
  for (const remaining of ['NaN', '-1', ''])
    assert.equal(
      accountCapacityView({ ...room, remaining }, 5, 101, false).ready,
      false,
    );
});
