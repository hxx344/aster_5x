import assert from 'node:assert/strict';
import { test } from 'node:test';
import { createStateHistory } from '../lib/state-history.ts';

test('deleting the selected or last account changes selection and drops its cached history', () => {
  const history = createStateHistory();
  history.merge([
    { id: 'first', cycle_trades: ['private'], cycle_trades_revision: 'r1' },
    { id: 'second' },
  ]);
  history.select('first');
  history.merge([{ id: 'second' }]);
  assert.equal(history.selected(), 'second');
  assert.match(history.url('first'), /history_revision=$/);
  history.merge([]);
  assert.equal(history.selected(), '');
  assert.equal(history.url(), '/api/state?compact=true');
});

test('unchanged history keeps its reference only for the same account and revision', () => {
  const history = createStateHistory();
  const trades = [{ trade_id: 'one' }];
  history.merge([
    { id: 'first', cycle_trades: trades, cycle_trades_revision: 'r1' },
    { id: 'second' },
  ]);
  const next = history.merge([
    { id: 'first', cycle_trades_revision: 'r1', balance: '200' },
    { id: 'second', cycle_trades_revision: 'r1' },
  ]);
  assert.equal(next[0].cycle_trades, trades);
  assert.equal(next[0].balance, '200');
  assert.equal(next[1].cycle_trades, undefined);
  assert.equal(
    history.merge([{ id: 'first', cycle_trades_revision: 'r2' }])[0]
      .cycle_trades,
    undefined,
  );
});

test('account switches request only that accounts known revision and accept a replacement', () => {
  const history = createStateHistory();
  history.merge([
    { id: 'first', cycle_trades: ['old'], cycle_trades_revision: 'r1' },
    { id: 'second', cycle_trades: ['other'], cycle_trades_revision: 'r2' },
  ]);
  assert.match(
    history.url('first'),
    /history_account=first&history_revision=r1/,
  );
  assert.match(
    history.url('second'),
    /history_account=second&history_revision=r2/,
  );
  assert.equal(history.url(''), '/api/state?compact=true');
  const next = history.merge([
    { id: 'first', cycle_trades: [], cycle_trades_revision: 'r3' },
  ]);
  assert.deepEqual(next[0].cycle_trades, []);
  assert.match(history.url('first'), /history_revision=r3/);
  assert.match(history.url('second'), /history_revision=$/);
});

test('logout drops all private history and loading never reuses an unconfirmed version', () => {
  const history = createStateHistory();
  history.merge([
    { id: 'first', cycle_trades: ['private'], cycle_trades_revision: 'r1' },
  ]);
  assert.equal(history.merge([{ id: 'first' }])[0].cycle_trades, undefined);
  history.clear();
  assert.equal(
    history.merge([{ id: 'first', cycle_trades_revision: 'r1' }])[0]
      .cycle_trades,
    undefined,
  );
  assert.match(history.url('first'), /history_revision=$/);
});
