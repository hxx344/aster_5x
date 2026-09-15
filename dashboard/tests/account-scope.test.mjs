import assert from 'node:assert/strict';
import { test } from 'node:test';
import { accountRunScope } from '../lib/account-scope.ts';

const policy = { symbols: ['XAUUSD1', 'SPCXUSD1', 'CLUSD1'], ordinary_symbol: 'all' };
test('account start scope excludes the cycle-owned symbol from ordinary opening', () => {
  assert.equal(accountRunScope({policy, cycle: {enabled: true, symbol: 'XAUUSD1'}}), '循环 XAUUSD1 · 普通开仓 SPCXUSD1 / CLUSD1');
  assert.equal(accountRunScope({policy: {...policy, ordinary_symbol: 'XAUUSD1'}, cycle: {enabled: true, symbol: 'XAUUSD1'}}), '循环 XAUUSD1');
});
test('ordinary selection and migration retain their distinct execution scopes', () => {
  assert.equal(accountRunScope({policy: {...policy, ordinary_symbol: 'CLUSD1'}}), '普通开仓 CLUSD1');
  assert.equal(accountRunScope({policy, migration: {enabled: true}}), 'XAU 迁移');
  assert.equal(accountRunScope({policy: {symbols: []}}), '没有可执行品种');
});
