import assert from 'node:assert/strict';
import { test } from 'node:test';
import { cycleDraft, cycleStatus, parseCycleDraft } from '../lib/cycle.ts';
import {
  cycleAmount,
  cycleDailySummary,
  cycleReportSummary,
  cycleTradeAction,
  cycleTradeDates,
  cycleTradesForDate,
  cycleTradeTimeSource,
  cycleUtcDate,
  cycleUtcTime,
  validCycleUtcDate,
} from '../lib/cycle-daily.ts';

const midnight = Date.parse('2026-09-14T00:00:00Z') / 1000;

test('background report distinguishes loading, age, refresh failure and UTC rollover', () => {
  const status = {
    status: 'ready',
    as_of: midnight + 100,
    max_age_seconds: 15,
    refreshing: false,
    error: null,
  };
  const current = cycleReportSummary(status, midnight + 101);
  assert.equal(current.stale, false);
  assert.match(current.notice, /2026-09-14 00:01:40 UTC/);
  assert.equal(cycleReportSummary(status, midnight + 115).stale, true);
  assert.equal(cycleReportSummary(status, midnight + 99).stale, true);
  assert.equal(
    cycleReportSummary({ ...status, as_of: midnight - 1 }, midnight).stale,
    true,
  );
  assert.match(
    cycleReportSummary({ ...status, refreshing: true }, midnight + 101).notice,
    /正在更新/,
  );
  const failed = cycleReportSummary(
    { ...status, error: 'retrying', status: 'stale' },
    midnight + 101,
  );
  assert.equal(failed.stale, true);
  assert.match(failed.notice, /更新失败，保留上次结果/);
  assert.match(
    cycleReportSummary({ ...status, as_of: null, status: 'loading' }, midnight)
      .notice,
    /等待首次结果/,
  );
  assert.deepEqual(cycleReportSummary(undefined, midnight), {
    stale: false,
    notice: '',
  });
});
const daily = {
  utc_date: '2026-09-13',
  volume: '39999.99999999999999',
  trade_count: 4,
  next_reset_at: midnight,
  limit: '40000',
  remaining: '0.00000000000001',
  reached: false,
};
const trade = {
  trade_id: 'fill-1',
  order_id: 'order-1',
  client_id: 'client-1',
  symbol: 'XAUUSD1',
  position_side: 'LONG',
  side: 'BUY',
  quantity: '2',
  price: '5000',
  notional: '10000',
  executed_at: midnight - 1,
  time_source: 'exchange',
  utc_date: '2026-09-13',
  daily_volume: '10000',
  intent_id: 'intent-1',
  phase: 'open',
};

test('daily volume limits preserve exact amounts and enforce the one trillion maximum', () => {
  assert.equal(parseCycleDraft(cycleDraft()).daily_volume_limit, '0');
  for (const value of [
    '0',
    '0.000',
    '40000',
    '0.0000000000000000001',
    '1000000000000',
    '1000000000000.000000000000000001',
  ]) {
    if (value === '1000000000000.000000000000000001') {
      assert.throws(
        () => parseCycleDraft({ ...cycleDraft(), daily_volume_limit: value }),
        /每日成交额度/,
      );
    } else {
      assert.equal(
        parseCycleDraft({ ...cycleDraft(), daily_volume_limit: value })
          .daily_volume_limit,
        value,
      );
    }
  }
  for (const value of [
    '',
    ' ',
    '-1',
    'NaN',
    'Infinity',
    '1000000000001',
    '1e20',
    '1'.repeat(41),
  ])
    assert.throws(
      () => parseCycleDraft({ ...cycleDraft(), daily_volume_limit: value }),
      /每日成交额度/,
    );
});

test('day assignment and reset labels use UTC at midnight, independent of browser local time', () => {
  assert.equal(cycleUtcDate(midnight - 0.001), '2026-09-13');
  assert.equal(cycleUtcDate(midnight), '2026-09-14');
  assert.equal(cycleUtcTime(midnight), '2026-09-14 00:00:00 UTC');
  assert.equal(
    cycleUtcDate(Date.parse('2026-09-14T08:00:00+08:00') / 1000),
    '2026-09-14',
  );
  assert.equal(
    cycleUtcDate(Date.parse('2026-09-13T17:00:00-07:00') / 1000),
    '2026-09-14',
  );
  assert.equal(validCycleUtcDate('2024-02-29'), true);
  for (const value of ['2026-02-29', '2026-13-14', '2026-9-14', '', undefined])
    assert.equal(validCycleUtcDate(value), false);
  for (const timestamp of [NaN, Infinity, -1, undefined, 9e15]) {
    assert.equal(cycleUtcDate(timestamp), null);
    assert.equal(cycleUtcTime(timestamp), '时间未知');
  }
});

test('a UTC rollover waits for the server and never invents a zero volume or a replenished allowance', () => {
  const before = cycleDailySummary(daily, midnight - 1);
  assert.equal(before.notice, '');
  const rollover = cycleDailySummary(daily, midnight);
  assert.equal(rollover.rolloverPending, true);
  assert.match(rollover.notice, /UTC 已换日/);
  assert.equal(rollover.volume, '39,999.99999999999999');
  assert.equal(rollover.remaining, '0.00000000000001');
  assert.equal(rollover.date, '2026-09-13');
  const refreshed = cycleDailySummary(
    {
      ...daily,
      utc_date: '2026-09-14',
      volume: '0',
      remaining: '40000',
      trade_count: 0,
      next_reset_at: midnight + 86400,
    },
    midnight + 1,
  );
  assert.equal(refreshed.notice, '');
  assert.equal(refreshed.volume, '0');
  assert.equal(refreshed.resetAt, '2026-09-15 00:00:00 UTC');
});

test('manual pause overrides a retained daily-limit state and never promises automatic restart', () => {
  const state = { phase: 'daily_limit', reason: '等待下一 UTC 日恢复' };
  assert.equal(cycleStatus(state, true, true).phase, 'daily_limit');
  const paused = cycleStatus(state, true, false);
  assert.equal(paused.phase, 'paused');
  assert.match(paused.reason, /仍需手动启动/);
  assert.doesNotMatch(paused.reason, /自动恢复/);
  assert.equal(cycleStatus(state, false, false).phase, 'disabled');
});

test('missing, stale, incomplete and failed daily statistics remain visibly uncertain', () => {
  const absent = cycleDailySummary(undefined, midnight);
  assert.equal(absent.volume, '—');
  assert.equal(absent.remaining, '—');
  assert.equal(absent.trades, '—');
  assert.match(absent.notice, /等待首次/);
  assert.match(cycleDailySummary(daily, midnight - 1, true).notice, /最近统计/);
  assert.match(
    cycleDailySummary({ ...daily, sync_pending: true }, midnight - 1).notice,
    /尚未确认/,
  );
  assert.match(
    cycleDailySummary({ ...daily, error: '成交读取失败' }, midnight - 1).notice,
    /成交读取失败/,
  );
  const unlimited = cycleDailySummary(
    { ...daily, limit: '0', remaining: null },
    midnight - 1,
  );
  assert.equal(unlimited.limit, '不限');
  assert.equal(unlimited.remaining, '不限');
  assert.match(
    cycleDailySummary({ ...daily, limit: '0', remaining: null, sync_pending: true }, midnight - 1).notice,
    /后台同步中，不影响循环/,
  );
  assert.match(
    cycleDailySummary({ ...daily, sync_pending: true, quota_pending: false, reserved_volume: '200' }, midnight - 1).notice,
    /剩余额度已扣除待补账预留/,
  );
  assert.equal(
    cycleDailySummary({ ...daily, remaining: undefined }, midnight - 1)
      .remaining,
    '—',
  );
});

test('individual amounts and server-provided daily cumulative values retain decimal precision', () => {
  assert.equal(
    cycleAmount('1000000000000.00000000000001'),
    '1,000,000,000,000.00000000000001',
  );
  assert.equal(cycleAmount('0.00000000000001'), '0.00000000000001');
  assert.equal(cycleAmount('10000.00'), '10,000.00');
  assert.equal(cycleAmount('0'), '0');
  for (const value of [undefined, null, '', 'NaN', 'Infinity', '-1'])
    assert.equal(cycleAmount(value), '—');
  const second = {
    ...trade,
    trade_id: 'fill-2',
    notional: '9999.99',
    daily_volume: '50123.45678901234567',
  };
  assert.equal(
    cycleAmount(cycleTradesForDate([trade, second], '')[1].daily_volume),
    '50,123.45678901234567',
  );
});

test('date filters use recorded UTC dates and never combine accounts or mutate loaded records', () => {
  const next = {
    ...trade,
    trade_id: 'fill-next',
    utc_date: '2026-09-14',
    executed_at: midnight,
    daily_volume: '1',
  };
  const trades = [next, trade];
  assert.deepEqual(cycleTradeDates(trades), ['2026-09-14', '2026-09-13']);
  assert.deepEqual(cycleTradesForDate(trades, '2026-09-14'), [next]);
  assert.deepEqual(cycleTradesForDate(trades, '2026-09-15'), []);
  assert.deepEqual(cycleTradesForDate(undefined, ''), []);
  assert.deepEqual(cycleTradeDates([{ ...trade, utc_date: undefined }]), []);
  assert.deepEqual(cycleTradesForDate([], ''), []);
  assert.deepEqual(trades, [next, trade]);
});

test('trade actions include repairs and estimated legacy times are explicitly identified', () => {
  assert.equal(cycleTradeAction(trade), '开仓 · 买入 · 多头');
  assert.equal(
    cycleTradeAction({ ...trade, side: 'SELL', phase: 'close' }),
    '平仓 · 卖出 · 多头',
  );
  assert.equal(
    cycleTradeAction({ ...trade, position_side: 'SHORT', phase: 'repair' }),
    '修复 · 买入 · 空头',
  );
  assert.equal(cycleTradeTimeSource(trade), '交易所成交时间');
  assert.equal(
    cycleTradeTimeSource({ ...trade, time_source: 'paper' }),
    '模拟成交时间',
  );
  assert.match(
    cycleTradeTimeSource({ ...trade, time_source: 'legacy_estimated' }),
    /估算/,
  );
  assert.equal(
    cycleTradeTimeSource({ ...trade, time_source: undefined }),
    '时间来源未知',
  );
});
