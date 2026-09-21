import assert from 'node:assert/strict';
import { test } from 'node:test';
import { accountSnapshotView } from '../lib/account-modes.ts';

const timestamp = 1_750_000_000;
const snapshot = { timestamp };
const paused = { interval_seconds: 60 };

test('scheduled records stop being fresh at eight seconds without declaring a refresh failure', () => {
  assert.equal(
    accountSnapshotView(snapshot, timestamp + 7.999, '', paused).fresh,
    true,
  );
  for (const age of [8, 30, 60, 67.999]) {
    const view = accountSnapshotView(snapshot, timestamp + age, '', paused);
    assert.equal(view.fresh, false);
    assert.equal(view.warning, false);
    assert.equal(view.label, '最近快照 · 定时更新');
    assert.match(view.notice, /60 秒/);
  }
});

test('overdue refresh warns after the budgeted interval plus the read freshness window', () => {
  for (const age of [68, 780, 3600]) {
    const view = accountSnapshotView(snapshot, timestamp + age, '', paused);
    assert.equal(view.warning, true);
    assert.match(view.notice, /长时间未更新/);
    assert.equal(view.fresh, false);
  }
  const slow = { interval_seconds: 120 };
  assert.equal(
    accountSnapshotView(snapshot, timestamp + 100, '', slow).warning,
    false,
  );
  assert.equal(
    accountSnapshotView(snapshot, timestamp + 120, '', slow).warning,
    true,
  );
  assert.equal(
    accountSnapshotView(snapshot, timestamp + 780, '', {
      interval_seconds: 900,
    }).warning,
    true,
  );
});

test('fast cycle refresh and unknown old servers retain the eight-second warning', () => {
  for (const refresh of [
    undefined,
    { interval_seconds: 2 },
    { interval_seconds: NaN },
    { interval_seconds: Infinity },
    { interval_seconds: -60 },
  ]) {
    assert.equal(
      accountSnapshotView(snapshot, timestamp + 8, '', refresh).warning,
      true,
    );
  }
});

test('connection and account errors are never hidden by the scheduled wait', () => {
  const disconnected = accountSnapshotView(
    snapshot,
    timestamp + 30,
    '连接超时',
    paused,
  );
  assert.equal(disconnected.warning, true);
  assert.match(disconnected.label, /连接异常/);
  const failed = accountSnapshotView(
    snapshot,
    timestamp + 30,
    '',
    paused,
    'Aster 网络连接失败',
  );
  assert.equal(failed.warning, true);
  assert.equal(failed.notice, 'Aster 网络连接失败');
  assert.equal(
    accountSnapshotView(snapshot, timestamp + 30, '', paused).warning,
    false,
  );
});

test('invalid and future timestamps cannot become scheduled or fresh records', () => {
  for (const value of [NaN, Infinity, 0, -1, 8.64e12 + 1, timestamp + 2]) {
    const view = accountSnapshotView(
      { timestamp: value },
      timestamp,
      '',
      paused,
    );
    assert.equal(view.fresh, false);
    assert.equal(view.warning, true);
    assert.match(view.notice, /时间异常/);
    assert.equal(view.elapsed, null);
  }
});

test('missing data and switching accounts do not invent or renew timestamps', () => {
  const empty = accountSnapshotView(undefined, timestamp, '', paused);
  assert.equal(empty.label, '等待账户数据');
  assert.equal(empty.elapsed, null);
  assert.equal(empty.fresh, false);
  const old = accountSnapshotView(snapshot, timestamp + 780, '', paused);
  assert.equal(old.elapsed, '13 分钟前');
  assert.equal(old.warning, true);
  const fresh = accountSnapshotView(
    { timestamp: timestamp + 780 },
    timestamp + 780,
    '',
    paused,
  );
  assert.equal(fresh.label, '已同步');
  assert.equal(
    accountSnapshotView(snapshot, timestamp + 780, '', paused).warning,
    true,
  );
});
