import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  ordinaryAddBlock,
  ordinaryCapacityReady,
} from '../lib/ordinary-add.ts';

test('ordinary selection follows account switches and reset and blocks capacity readiness', () => {
  const selected = { policy: { ordinary_symbol: 'CLUSD1' } };
  assert.equal(ordinaryAddBlock(selected, 'CLUSD1'), null);
  assert.match(ordinaryAddBlock(selected, 'XAUUSD1'), /仅限 CLUSD1/);
  assert.match(ordinaryAddBlock(selected, 'SPCXUSD1'), /禁止 SPCXUSD1/);
  assert.equal(
    ordinaryCapacityReady(
      '999999',
      '10000',
      true,
      ordinaryAddBlock(selected, 'XAUUSD1'),
    ),
    false,
  );
  assert.equal(
    ordinaryAddBlock({ policy: { ordinary_symbol: 'XAUUSD1' } }, 'XAUUSD1'),
    null,
  );
  selected.policy.ordinary_symbol = 'all';
  assert.equal(ordinaryAddBlock(selected, 'XAUUSD1'), null);
  assert.equal(ordinaryAddBlock({ policy: {} }, 'XAUUSD1'), null);
});

test('selected ordinary market remains blocked when owned by cycle', () => {
  assert.ok(
    ordinaryAddBlock(
      {
        policy: { ordinary_symbol: 'CLUSD1' },
        cycle: { enabled: true, symbol: 'CLUSD1' },
      },
      'CLUSD1',
    ),
  );
});

test('cycle ordinary-add restrictions stay with the selected account and symbol', () => {
  const cycleAccount = {
    ordinary_add_blocks: { XAUUSD1: '当前账户循环占用品种，普通加仓已禁用' },
    cycle: { enabled: true, symbol: 'XAUUSD1' },
  };
  const ordinaryAccount = {
    ordinary_add_blocks: {},
    cycle: { enabled: false, symbol: 'XAUUSD1' },
  };
  assert.equal(
    ordinaryAddBlock(cycleAccount, 'XAUUSD1'),
    cycleAccount.ordinary_add_blocks.XAUUSD1,
  );
  assert.equal(ordinaryAddBlock(cycleAccount, 'CLUSD1'), null);
  assert.equal(ordinaryAddBlock(ordinaryAccount, 'XAUUSD1'), null);
  assert.ok(ordinaryAddBlock(cycleAccount, 'XAUUSD1'));
  assert.equal(ordinaryAddBlock(undefined, 'XAUUSD1'), null);
});

test('pausing an account preserves its cycle block until cycle is disabled', () => {
  const account = {
    enabled: false,
    cycle: { enabled: true, symbol: 'CLUSD1' },
  };
  assert.ok(ordinaryAddBlock(account, 'CLUSD1'));
  assert.equal(ordinaryAddBlock(account, 'XAUUSD1'), null);
  account.enabled = true;
  assert.ok(ordinaryAddBlock(account, 'CLUSD1'));
  account.cycle.enabled = false;
  assert.equal(ordinaryAddBlock(account, 'CLUSD1'), null);
});

test('old API fallback uses only the current account cycle configuration', () => {
  assert.equal(ordinaryAddBlock({}, 'XAUUSD1'), null);
  assert.equal(
    ordinaryAddBlock(
      { cycle: { enabled: false, symbol: 'XAUUSD1' } },
      'XAUUSD1',
    ),
    null,
  );
  assert.ok(
    ordinaryAddBlock(
      { cycle: { enabled: true, symbol: 'XAUUSD1' } },
      'XAUUSD1',
    ),
  );
  assert.equal(
    ordinaryAddBlock({ ordinary_add_blocks: { XAUUSD1: '  ' } }, 'XAUUSD1'),
    null,
  );
});

test('sufficient 5x, 10x and 20x public capacities never override an account block', () => {
  const capacities = { 5: '100001', 10: '200002', 20: '300003' };
  for (const capacity of Object.values(capacities)) {
    assert.equal(
      ordinaryCapacityReady(capacity, '10000', true, '循环占用'),
      false,
    );
    assert.equal(ordinaryCapacityReady(capacity, '10000', true, null), true);
    assert.equal(ordinaryCapacityReady(capacity, '10000', false, null), false);
  }
  assert.deepEqual(capacities, { 5: '100001', 10: '200002', 20: '300003' });
  assert.equal(ordinaryCapacityReady('10000', '10000', true, null), false);
  assert.equal(ordinaryCapacityReady(undefined, '10000', true, null), false);
});
