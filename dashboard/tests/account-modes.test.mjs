import assert from 'node:assert/strict';
import { test } from 'node:test';
import { accountModeView } from '../lib/account-modes.ts';

const timestamp = 1_750_000_000;
const keys = ['cross', 'hedge', 'single_asset'];
const passing = {
  timestamp,
  mode_checks: { cross: true, hedge: true, single_asset: true },
};
const result = (view, key) => {
  const row = view.rows.find((item) => item.key === key);
  assert.ok(row, `missing mode row: ${key}`);
  return { value: row.value, failed: row.failed };
};
const assertUnknown = (view) => {
  for (const key of keys)
    assert.deepEqual(result(view, key), {
      value: '尚未核验',
      failed: false,
    });
};

test('each mode reports only its own strict boolean verification result', () => {
  const view = accountModeView(
    { timestamp, mode_checks: { cross: true, hedge: false } },
    timestamp + 1,
  );
  assert.equal(view.hasSnapshot, true);
  assert.equal(view.rows.length, 3);
  assert.deepEqual(new Set(view.rows.map((row) => row.key)), new Set(keys));
  for (const row of view.rows) assert.ok(row.label.trim());
  assert.deepEqual(result(view, 'cross'), {
    value: '上次核实通过',
    failed: false,
  });
  assert.deepEqual(result(view, 'hedge'), {
    value: '不符合要求',
    failed: true,
  });
  assert.deepEqual(result(view, 'single_asset'), {
    value: '尚未核验',
    failed: false,
  });
});

test('crossing the eight-second freshness limit preserves the recorded results', () => {
  const current = accountModeView(passing, timestamp + 7.999);
  const stale = accountModeView(passing, timestamp + 8);
  assert.equal(current.notice, '');
  assert.match(stale.notice, /账户数据已过期/);
  assert.deepEqual(stale.rows, current.rows);
  assert.equal(stale.recordedAt, current.recordedAt);
  for (const key of keys)
    assert.equal(result(stale, key).value, '上次核实通过');
});

test('known mode failures remain failures when data expires or the connection fails', () => {
  const snapshot = {
    timestamp,
    mode_checks: { cross: false, hedge: false, single_asset: false },
  };
  for (const [age, connectionError] of [
    [8, undefined],
    [1, '连接超时'],
    [90, '连接中断'],
  ]) {
    const view = accountModeView(snapshot, timestamp + age, connectionError);
    assert.match(view.notice, connectionError ? /连接异常/ : /账户数据已过期/);
    for (const key of keys)
      assert.deepEqual(result(view, key), {
        value: '不符合要求',
        failed: true,
      });
  }
});

test('missing, null, numeric and string mode fields never imply verification', () => {
  for (const value of [undefined, null, 'true', 'false', '', 0, 1]) {
    const view = accountModeView(
      {
        timestamp,
        mode_checks: Object.fromEntries(keys.map((key) => [key, value])),
      },
      timestamp,
    );
    assertUnknown(view);
  }
  for (const mode_checks of [undefined, null, {}]) {
    const view = accountModeView({ timestamp, mode_checks }, timestamp);
    assert.equal(view.hasSnapshot, true);
    assertUnknown(view);
  }
});

test('absent snapshots have no invented record time, age or stale-data warning', () => {
  for (const connectionError of [undefined, '连接中断']) {
    const view = accountModeView(undefined, timestamp, connectionError);
    assert.equal(view.hasSnapshot, false);
    assert.equal(view.recordedAt, null);
    assert.equal(view.elapsed, null);
    assertUnknown(view);
    if (connectionError) assert.match(view.notice, /连接异常/);
    else assert.equal(view.notice, '');
    assert.doesNotMatch(view.notice, /已过期|时间异常/);
  }
});

test('invalid record dates preserve mode results without rendering an invalid date or age', () => {
  for (const invalid of [
    undefined,
    NaN,
    Infinity,
    -Infinity,
    0,
    -1,
    8.64e12 + 1,
  ]) {
    const view = accountModeView({ ...passing, timestamp: invalid }, timestamp);
    assert.equal(view.hasSnapshot, true);
    assert.equal(view.recordedAt, null);
    assert.equal(view.elapsed, null);
    assert.match(view.notice, /账户快照时间异常/);
    assert.doesNotMatch(JSON.stringify(view), /Invalid Date|NaN|Infinity/);
    for (const key of keys)
      assert.equal(result(view, key).value, '上次核实通过');
  }
});

test('future record times outside clock tolerance never render a negative elapsed age', () => {
  const tolerated = accountModeView(passing, timestamp - 1);
  assert.equal(tolerated.notice, '');
  assert.match(tolerated.elapsed, /^0\s*秒(?:前)?$/);

  const future = accountModeView(passing, timestamp - 1.001);
  assert.match(future.notice, /账户快照时间异常/);
  assert.equal(future.elapsed, null);
  assert.equal(typeof future.recordedAt, 'string');
  assert.doesNotMatch(future.recordedAt, /Invalid Date/);
  assert.deepEqual(future.rows, tolerated.rows);

  const invalidNow = accountModeView(passing, NaN);
  assert.match(invalidNow.notice, /账户快照时间异常/);
  assert.equal(invalidNow.elapsed, null);
});

test('elapsed labels round down at second, minute, hour and day boundaries', () => {
  for (const [age, value, unit] of [
    [-1, 0, '秒'],
    [0, 0, '秒'],
    [0.999, 0, '秒'],
    [1, 1, '秒'],
    [59.999, 59, '秒'],
    [60, 1, '分钟'],
    [119.999, 1, '分钟'],
    [3599.999, 59, '分钟'],
    [3600, 1, '小时'],
    [86399.999, 23, '小时'],
    [86400, 1, '天'],
    [172799.999, 1, '天'],
  ]) {
    const view = accountModeView(passing, timestamp + age);
    assert.match(
      view.elapsed,
      new RegExp(`^${value}\\s*${unit}(?:前)?$`),
      `elapsed age ${age}`,
    );
  }
});

test('record time contains a local date and time without assuming the test timezone', () => {
  const view = accountModeView(passing, timestamp);
  const local = new Date(timestamp * 1000);
  assert.equal(typeof view.recordedAt, 'string');
  assert.deepEqual(view.recordedAt.match(/\d+/g).slice(0, 3).map(Number), [
    local.getFullYear(),
    local.getMonth() + 1,
    local.getDate(),
  ]);
  assert.match(view.recordedAt, /\d{1,2}:\d{2}:\d{2}/);
  assert.doesNotMatch(view.recordedAt, /Invalid Date/);
});

test('switching accounts never carries another accounts mode results into its snapshot', () => {
  const first = accountModeView(passing, timestamp + 1);
  const second = accountModeView(
    { timestamp, mode_checks: { cross: false } },
    timestamp + 1,
  );
  assert.deepEqual(result(second, 'cross'), {
    value: '不符合要求',
    failed: true,
  });
  for (const key of ['hedge', 'single_asset'])
    assert.deepEqual(result(second, key), {
      value: '尚未核验',
      failed: false,
    });
  const empty = accountModeView(undefined, timestamp + 1);
  assertUnknown(empty);
  assert.equal(empty.recordedAt, null);
  assert.equal(empty.hasSnapshot, false);
  assert.deepEqual(accountModeView(passing, timestamp + 1), first);
});
