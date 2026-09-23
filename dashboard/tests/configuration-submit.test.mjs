import assert from 'node:assert/strict';
import { test } from 'node:test';
import { submitConfiguration } from '../lib/configuration-submit.ts';
import { draftFields } from '../lib/use-account-draft.ts';

function fixture(overrides = {}) {
  const events = [];
  const settings = {
    account: { id: 'one' },
    locked: false,
    changes: () => ({ cycle: { hold_seconds: 90 } }),
    action: async (...args) => {
      events.push(['request', ...args]);
      return true;
    },
    clearDraft: () => events.push(['clear', 'one']),
    setNotice: (value) => events.push(['notice', value]),
    setError: (value) => events.push(['error', value]),
    success: 'saved',
    errorFallback: 'invalid',
    ...overrides,
  };
  return { settings, events };
}

test('locked configuration never parses or sends; rejected save retains its draft', async () => {
  const locked = fixture({
    locked: true,
    changes: () => {
      throw new Error('must not parse');
    },
  });
  await submitConfiguration(locked.settings);
  assert.deepEqual(locked.events, []);
  const rejected = fixture({ action: async () => false });
  await submitConfiguration(rejected.settings);
  assert.deepEqual(rejected.events, []);
});

test('successful saves preserve nested and flat requests and clear only after acknowledgement', async () => {
  for (const body of [
    { cycle: { enabled: false } },
    { migration: { batch_notional: '500' } },
    { margin_limit: '0.55' },
  ]) {
    const { settings, events } = fixture({ changes: () => body });
    await submitConfiguration(settings);
    assert.deepEqual(events, [
      ['request', '/api/accounts/one', body, 'PATCH'],
      ['clear', 'one'],
      ['notice', 'saved'],
    ]);
  }
});

test('parse failures retain draft and preserve the risk forms distinct notice handling', async () => {
  for (const clearNoticeOnError of [true, false]) {
    const { settings, events } = fixture({
      clearNoticeOnError,
      changes: () => {
        throw new Error('precise boundary');
      },
    });
    await submitConfiguration(settings);
    assert.deepEqual(events, [
      ...(clearNoticeOnError ? [['notice', '']] : []),
      ['error', 'precise boundary'],
    ]);
  }
  const fallback = fixture({
    changes: () => {
      throw 'not an Error';
    },
  });
  await submitConfiguration(fallback.settings);
  assert.deepEqual(fallback.events, [
    ['notice', ''],
    ['error', 'invalid'],
  ]);
});

test('an in-flight save keeps the submitted account callbacks after account selection changes', async () => {
  let finish;
  const first = fixture({
    action: () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  });
  let visible = first.settings;
  const saving = submitConfiguration(visible);
  const second = fixture({
    account: { id: 'two' },
    clearDraft: () => second.events.push(['clear', 'two']),
  });
  visible = second.settings;
  finish(true);
  await saving;
  assert.equal(visible.account.id, 'two');
  assert.deepEqual(first.events, [
    ['clear', 'one'],
    ['notice', 'saved'],
  ]);
  assert.deepEqual(second.events, []);
});

test('field changes preserve exact decimal text, sibling values and the original draft', () => {
  const saved = {
    amount: '0.100000000000000001',
    unit: 'seconds',
    enabled: false,
  };
  let next;
  const fields = draftFields(saved, (value) => {
    next = value;
  });
  fields('amount').onValueChange('100.000000000000000001');
  assert.deepEqual(next, { ...saved, amount: '100.000000000000000001' });
  assert.equal(saved.amount, '0.100000000000000001');
  assert.equal(fields('unit').value, 'seconds');
});
