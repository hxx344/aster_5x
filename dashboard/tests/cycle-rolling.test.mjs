import assert from 'node:assert/strict';
import { test } from 'node:test';
import { cycleStatus } from '../lib/cycle.ts';
import { cycleDailySummary, cycleRollingSummary } from '../lib/cycle-daily.ts';

const midnight = Date.parse('2026-09-15T00:00:00Z') / 1000;
const rolling = {
  window_start: midnight - 86400,
  window_end: midnight,
  volume: '39999.99999999999999',
  trade_count: 4,
  next_release_at: midnight + 3600,
  estimated_volume: '0',
  estimated_trade_count: 0,
  limit: '40000',
  remaining: '0.00000000000001',
  reached: false,
};

test('UTC midnight resets only the daily display and leaves the reported rolling usage intact', () => {
  const day = cycleDailySummary(
    {
      utc_date: '2026-09-15',
      volume: '0',
      trade_count: 0,
      next_reset_at: midnight + 86400,
      limit: '40000',
      remaining: '40000',
      reached: false,
    },
    midnight + 1,
  );
  const view = cycleRollingSummary(rolling, midnight + 1);
  assert.equal(day.volume, '0');
  assert.equal(day.remaining, '40,000');
  assert.equal(view.volume, '39,999.99999999999999');
  assert.equal('remaining' in view, false);
  assert.equal(view.releaseAt, '2026-09-15 01:00:00 UTC');
  assert.equal(
    cycleStatus({ phase: 'rolling_limit' }, true, true).label,
    '等待开仓',
  );
});

test('the next release never zeroes usage or promises enough capacity to resume', () => {
  const value = {
    ...rolling,
    window_start: midnight - 86400,
    window_end: midnight,
    next_release_at: midnight + 1,
  };
  const before = cycleRollingSummary(value, midnight);
  assert.equal(before.releasePassed, false);
  const due = cycleRollingSummary(value, midnight + 1);
  assert.equal(due.releasePassed, true);
  assert.match(due.notice, /等待服务更新统计/);
  assert.equal(due.volume, before.volume);
  assert.doesNotMatch(due.notice, /恢复/);
});

test('rolling freshness ages from window_end even if the surrounding account response is fresh', () => {
  assert.equal(cycleRollingSummary(rolling, midnight + 7.999).stale, false);
  for (const [now, outerStale] of [
    [midnight + 8, false],
    [midnight + 1, true],
    [midnight - 2, false],
  ]) {
    const view = cycleRollingSummary(rolling, now, outerStale);
    assert.equal(view.stale, true);
    assert.match(view.notice, /过期/);
    assert.equal(view.volume, '39,999.99999999999999');
  }
  assert.match(
    cycleRollingSummary({ ...rolling, window_start: midnight }, midnight)
      .notice,
    /窗口未知或异常/,
  );
  assert.match(
    cycleRollingSummary({ ...rolling, window_end: NaN }, midnight).notice,
    /窗口未知或异常/,
  );
});

test('missing or incomplete rolling records do not appear as fresh zero capacity use', () => {
  const absent = cycleRollingSummary(undefined, midnight);
  assert.equal(absent.volume, '—');
  assert.equal(absent.windowEnd, '时间未知');
  assert.match(absent.notice, /等待首次/);
  assert.match(
    cycleRollingSummary({ ...rolling, sync_pending: true }, midnight).notice,
    /尚未确认/,
  );
  assert.match(
    cycleRollingSummary({ ...rolling, error: '未获取成交' }, midnight).notice,
    /未获取成交/,
  );
  const unlimited = cycleRollingSummary(
    { ...rolling, limit: '0', remaining: null, next_release_at: null },
    midnight,
  );
  assert.equal(unlimited.releaseAt, '暂无待移出成交');
});

test('manual pause overrides both quota phases after midnight or a rolling release', () => {
  for (const phase of ['daily_limit', 'rolling_limit']) {
    const view = cycleStatus({ phase, reason: '自动恢复新增' }, true, false);
    assert.equal(view.phase, 'paused');
    assert.match(view.reason, /仍需手动启动/);
    assert.doesNotMatch(view.reason, /自动恢复/);
    assert.equal(cycleStatus({ phase }, false, true).phase, 'disabled');
  }
});

test('rolling statistics remain account-specific and visibly label estimated old fills', () => {
  const first = cycleRollingSummary(rolling, midnight);
  const second = cycleRollingSummary(
    {
      ...rolling,
      volume: '50',
      remaining: '39950',
      trade_count: 1,
      estimated_volume: '50',
    },
    midnight,
  );
  assert.equal(first.hasEstimates, false);
  assert.equal(second.volume, '50');
  assert.equal(second.hasEstimates, true);
  assert.equal(second.estimatedVolume, '50');
  assert.deepEqual(cycleRollingSummary(rolling, midnight), first);
});
