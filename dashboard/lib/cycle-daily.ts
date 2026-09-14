import type { CycleDailyVolume, CycleTrade } from './cycle';

// Keep monetary strings exact, including cumulative values above Number's
// precise range. The server owns aggregation and UTC day assignment.
export function cycleAmount(value: string | null | undefined): string {
  if (typeof value !== 'string') return '—';
  const match = /^(\d+)(?:\.(\d+))?$/.exec(value);
  if (!match) return '—';
  const whole = (match[1].replace(/^0+(?=\d)/, '') || '0').replace(
    /\B(?=(\d{3})+(?!\d))/g,
    ',',
  );
  return match[2] ? `${whole}.${match[2]}` : whole;
}

export function cycleUtcDate(timestamp: number | undefined): string | null {
  if (
    typeof timestamp !== 'number' ||
    !Number.isFinite(timestamp) ||
    timestamp < 0
  )
    return null;
  const value = new Date(timestamp * 1000);
  return Number.isFinite(value.getTime())
    ? value.toISOString().slice(0, 10)
    : null;
}

export function cycleUtcTime(timestamp: number | undefined): string {
  if (!cycleUtcDate(timestamp)) return '时间未知';
  return `${new Date(timestamp! * 1000).toISOString().slice(0, 19).replace('T', ' ')} UTC`;
}

export function validCycleUtcDate(value: string | undefined): value is string {
  return (
    typeof value === 'string' &&
    /^\d{4}-\d{2}-\d{2}$/.test(value) &&
    cycleUtcDate(Date.parse(`${value}T00:00:00Z`) / 1000) === value
  );
}

export function cycleDailySummary(
  daily: CycleDailyVolume | undefined,
  now: number,
  stale = false,
) {
  const date = validCycleUtcDate(daily?.utc_date) ? daily.utc_date : null;
  const currentDate = cycleUtcDate(now);
  const rolloverPending = Boolean(date && currentDate && date !== currentDate);
  const unlimited =
    typeof daily?.limit === 'string' && /^0+(?:\.0+)?$/.test(daily.limit);
  const notice = !daily
    ? '等待首次每日成交统计'
    : daily.error
      ? `成交统计异常：${daily.error}`
      : daily.sync_pending
        ? '正在核对成交，额度尚未确认'
        : !date
          ? '统计日期未知，等待刷新'
          : rolloverPending
            ? 'UTC 已换日，等待服务确认新一天统计'
            : stale
              ? '数据过期，以下为最近统计'
              : '';
  return {
    date: date ?? '日期未知',
    volume: cycleAmount(daily?.volume),
    limit: unlimited ? '不限' : cycleAmount(daily?.limit),
    remaining:
      unlimited && daily?.remaining === null
        ? '不限'
        : cycleAmount(daily?.remaining),
    trades:
      typeof daily?.trade_count === 'number' &&
      Number.isSafeInteger(daily.trade_count) &&
      daily.trade_count >= 0
        ? String(daily.trade_count)
        : '—',
    resetAt: daily ? cycleUtcTime(daily.next_reset_at) : '等待服务确认',
    notice,
    rolloverPending,
  };
}

const phases: Record<string, string> = {
  open: '开仓',
  close: '平仓',
  repair: '修复',
};
const directions: Record<string, string> = { BUY: '买入', SELL: '卖出' };
const positions: Record<string, string> = { LONG: '多头', SHORT: '空头' };

export function cycleTradeAction(trade: CycleTrade): string {
  return [
    phases[trade.phase] || '动作未知',
    directions[trade.side] || '方向未知',
    positions[trade.position_side] || '持仓方向未知',
  ].join(' · ');
}

export function cycleTradeTimeSource(trade: CycleTrade): string {
  return trade.time_source === 'exchange'
    ? '交易所成交时间'
    : trade.time_source === 'paper'
      ? '模拟成交时间'
      : trade.time_source === 'legacy_estimated'
        ? '估算时间 · 旧模拟单'
        : '时间来源未知';
}

export function cycleTradeDates(trades: CycleTrade[] | undefined): string[] {
  return [
    ...new Set(
      (trades || []).map((trade) => trade.utc_date).filter(validCycleUtcDate),
    ),
  ]
    .sort()
    .reverse();
}

export function cycleTradesForDate(
  trades: CycleTrade[] | undefined,
  date: string,
): CycleTrade[] {
  return (trades || []).filter((trade) => !date || trade.utc_date === date);
}
