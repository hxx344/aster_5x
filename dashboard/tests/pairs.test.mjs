import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  availablePairAccounts,
  pairConfigurationChanges,
  pairConfigurationLock,
  pairDataFresh,
  pairDeletionBlock,
  pairDraft,
  pairHasPending,
  pairMarginStatus,
  pairNetQuantity,
  pairSnapshotStatus,
  pairTransferStatus,
  parsePairDraft,
} from '../lib/pairs.ts';

const accounts = [
  { id: 'a', name: 'A', mode: 'paper', enabled: false },
  { id: 'b', name: 'B', mode: 'paper', enabled: false },
];
const draft = () => ({
  ...pairDraft(),
  id: 'gold_pair',
  name: '黄金配对',
  long_account_id: 'a',
  short_account_id: 'b',
});
const snapshot = (positions = [], timestamp = 100) => ({
  positions,
  timestamp,
});
const position = (side, qty, symbol = 'XAUUSD1') => ({ side, qty, symbol });
const pair = (state = {}) => ({
  ...parsePairDraft(draft(), accounts, []),
  enabled: false,
  state: { snapshots: { long: snapshot(), short: snapshot() }, ...state },
});

test('pair requests preserve decimal strings and fixed roles without enabling the group', () => {
  const config = parsePairDraft(
    {
      ...draft(),
      mode: 'ordinary',
      order_notional: '1000.000000001',
      margin_percent: '50.00001',
    },
    accounts,
    [],
  );
  assert.equal(config.symbol, 'XAUUSD1');
  assert.equal(config.long_account_id, 'a');
  assert.equal(config.short_account_id, 'b');
  assert.equal(config.ordinary.order_notional, '1000.000000001');
  assert.equal(config.ordinary.margin_limit, '0.5000001');
  assert.equal(config.ordinary.enabled, true);
  assert.equal(config.cycle.enabled, false);
  assert.equal(Object.hasOwn(config, 'enabled'), false);
  assert.equal(typeof config.margin.check_interval_seconds, 'number');
  assert.deepEqual(Object.keys(pairConfigurationChanges(config)).sort(), [
    'cycle',
    'margin',
    'name',
    'ordinary',
  ]);
});

test('trade mode is exclusive and monitor-only does not silently enable trading', () => {
  for (const mode of ['monitor', 'ordinary', 'cycle']) {
    const config = parsePairDraft({ ...draft(), mode }, accounts, []);
    assert.equal(config.ordinary.enabled, mode === 'ordinary');
    assert.equal(config.cycle.enabled, mode === 'cycle');
  }
});

test('membership rejects duplicates, mixed environments, occupied and running accounts', () => {
  assert.throws(
    () => parsePairDraft({ ...draft(), short_account_id: 'a' }, accounts, []),
    /不同/,
  );
  assert.throws(
    () =>
      parsePairDraft(
        draft(),
        [accounts[0], { ...accounts[1], mode: 'live' }],
        [],
      ),
    /相同/,
  );
  assert.throws(
    () => parsePairDraft(draft(), accounts, [{ ...pair(), id: 'other' }]),
    /占用/,
  );
  assert.throws(
    () =>
      parsePairDraft(
        draft(),
        [{ ...accounts[0], enabled: true }, accounts[1]],
        [],
      ),
    /暂停/,
  );
  assert.deepEqual(
    availablePairAccounts(
      [{ ...accounts[0], pair_id: 'unlisted' }, accounts[1]],
      [],
    ).map((account) => account.id),
    ['b'],
  );
  assert.equal(
    availablePairAccounts(accounts, [pair()], 'gold_pair').length,
    2,
  );
});

test('live automatic transfers need a master prefix and meaningful transfer bounds', () => {
  const live = accounts.map((account) => ({ ...account, mode: 'live' }));
  assert.throws(
    () => parsePairDraft({ ...draft(), margin_enabled: true }, live, []),
    /主账户凭据/,
  );
  assert.throws(
    () => parsePairDraft({ ...draft(), min_transfer: '1001' }, accounts, []),
    /不能大于/,
  );
  assert.throws(
    () =>
      parsePairDraft(
        { ...draft(), check_interval_seconds: '1.5' },
        accounts,
        [],
      ),
    /整数秒/,
  );
  assert.throws(
    () => parsePairDraft({ ...draft(), buffer_percent: '100' }, accounts, []),
    /小于 100/,
  );
  assert.throws(
    () => parsePairDraft({ ...draft(), order_notional: '499' }, accounts, []),
    /500/,
  );
  assert.throws(
    () => parsePairDraft({ ...draft(), spread_limit: '0' }, accounts, []),
    /大于零/,
  );
  assert.throws(
    () => parsePairDraft({ ...draft(), spread_limit: '0.001' }, accounts, []),
    /不能超过/,
  );
  assert.throws(
    () =>
      parsePairDraft(
        { ...draft(), check_interval_seconds: '86401' },
        accounts,
        [],
      ),
    /86400/,
  );
  assert.throws(
    () =>
      parsePairDraft({ ...draft(), max_transfer: 'Infinity' }, accounts, []),
    /金额或数值/,
  );
  const config = parsePairDraft(
    {
      ...draft(),
      margin_enabled: true,
      master_env_prefix: 'ASTER_MASTER',
      buffer_percent: '0',
    },
    live,
    [],
  );
  assert.equal(config.margin.buffer_ratio, '0');
  assert.equal(config.margin.master_env_prefix, 'ASTER_MASTER');
});

test('every live group requires an independent master prefix on create and edit even when balancing is disabled', () => {
  const live = accounts.map((account) => ({
    ...account,
    mode: 'live',
    env_prefix: `ASTER_${account.id.toUpperCase()}`,
  }));
  for (const mode of ['monitor', 'ordinary', 'cycle']) {
    for (const currentId of [undefined, 'gold_pair']) {
      const input = { ...draft(), mode, margin_enabled: false };
      assert.throws(
        () => parsePairDraft(input, live, [], currentId),
        /实盘配对组须填写独立主账户/,
      );
      for (const master_env_prefix of ['ASTER_A', 'ASTER_B']) {
        assert.throws(
          () =>
            parsePairDraft(
              { ...input, master_env_prefix },
              live,
              [],
              currentId,
            ),
          /不能使用任一子账户/,
        );
      }
      const saved = parsePairDraft(
        { ...input, master_env_prefix: 'ASTER_MASTER' },
        live,
        [],
        currentId,
      );
      assert.equal(saved.margin.enabled, false);
      assert.equal(saved.margin.master_env_prefix, 'ASTER_MASTER');
    }
  }
  assert.equal(
    parsePairDraft(draft(), accounts, []).margin.master_env_prefix,
    '',
  );
});

test('transfer statuses distinguish exchange acknowledgement, refreshed balances and independently verified income', () => {
  const acknowledged = {
    request_id: 'r',
    source: 'long',
    destination: 'short',
    amount: '10',
    created_at: 90,
    status: 'acknowledged',
    acknowledged_at: 100,
  };
  assert.match(pairTransferStatus(acknowledged), /等待余额刷新/);
  assert.match(
    pairMarginStatus({
      status: 'acknowledged',
      pending: acknowledged,
      last_transfer: acknowledged,
    }),
    /等待余额刷新/,
  );
  const refreshed = { ...acknowledged, refreshed_at: 101 };
  assert.equal(pairTransferStatus(refreshed), '交易所回执确认，余额已刷新');
  assert.equal(
    pairMarginStatus({
      status: 'acknowledged',
      pending: null,
      last_transfer: refreshed,
    }),
    '交易所回执确认，余额已刷新',
  );
  assert.equal(
    pairTransferStatus({ ...refreshed, status: 'refreshed' }),
    '交易所回执确认，余额已刷新',
  );
  assert.match(
    pairTransferStatus({ ...refreshed, status: 'confirmed' }),
    /两侧划转流水已核实/,
  );
  assert.match(
    pairTransferStatus({ ...refreshed, status: 'unknown' }),
    /结果未知/,
  );
  assert.match(
    pairTransferStatus({ ...acknowledged, refreshed_at: NaN }),
    /等待余额刷新/,
  );
});

test('snapshot freshness handles exact 8-second boundary, offline and invalid clocks', () => {
  assert.equal(pairDataFresh(100, 107.99), true);
  for (const [time, now, offline] of [
    [100, 108, false],
    [100, 101, true],
    [undefined, 100, false],
    [102, 100, false],
    [100, NaN, false],
  ])
    assert.equal(pairDataFresh(time, now, offline), false);
  assert.equal(pairSnapshotStatus(undefined, 100), '等待账户数据');
  assert.match(pairSnapshotStatus(snapshot(), 108), /过期/);
  assert.match(pairSnapshotStatus(snapshot(), 101, true), /连接中断/);
});

test('quantity difference stays exact and missing side never becomes zero', () => {
  assert.equal(pairNetQuantity({ long: snapshot(), short: null }), null);
  assert.equal(pairNetQuantity({ long: {}, short: snapshot() }), null);
  assert.equal(
    pairNetQuantity({
      long: snapshot([position('LONG', '0.300000000000000001')]),
      short: snapshot([position('SHORT', '-0.3')]),
    }),
    '0.000000000000000001',
  );
  assert.equal(
    pairNetQuantity({
      long: snapshot([position('LONG', '1'), position('SHORT', '0.1')]),
      short: snapshot([position('SHORT', '1')]),
    }),
    '-0.1',
  );
  assert.equal(
    pairNetQuantity({
      long: snapshot([position('LONG', '8', 'BTCUSDT')]),
      short: snapshot(),
    }),
    '0',
  );
  assert.equal(
    pairNetQuantity({
      long: snapshot([position('LONG', 'bad')]),
      short: snapshot(),
    }),
    null,
  );
  assert.equal(pairNetQuantity({ long: snapshot(), short: snapshot() }), '0');
});

test('unknown orders and transfers block edits/deletion without claiming completion', () => {
  for (const state of [
    { pending: { orders: [{ id: 'pending' }] } },
    { margin: { status: 'unknown' } },
    { margin: { pending: { request_id: 'r' } } },
  ]) {
    assert.equal(pairHasPending(pair(state)), true);
    assert.match(pairConfigurationLock(pair(state)), /待核对/);
    assert.match(pairDeletionBlock(pair(state), 100), /等待确认/);
  }
  assert.match(
    pairConfigurationLock(pair({ progress: { quantities: { LONG: '0.01' } } })),
    /基线/,
  );
});

test('deletion blocks known positions and defers missing/stale snapshots to the server fresh-read guard', () => {
  assert.equal(pairDeletionBlock(pair(), 100), '');
  assert.match(pairDeletionBlock({ ...pair(), enabled: true }, 100), /暂停/);
  assert.equal(pairDeletionBlock(pair(), 108), '');
  assert.equal(
    pairDeletionBlock(
      pair({ snapshots: { long: snapshot(), short: null } }),
      100,
    ),
    '',
  );
  assert.match(pairDeletionBlock(pair(), 100, true), /连接恢复/);
  assert.match(
    pairDeletionBlock(
      pair({
        snapshots: {
          long: snapshot(),
          short: snapshot([position('LONG', '1', 'BTCUSDT')]),
        },
      }),
      100,
    ),
    /所有仓位/,
  );
});
