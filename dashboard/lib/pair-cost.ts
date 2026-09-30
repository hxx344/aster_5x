import { cycleAmount, cycleSignedAmount, cycleUtcDate } from './cycle-daily.ts';

export type PairCostPeriod = {
  start: number;
  end: number;
  label: string;
  estimated_fee: string;
  spread_cost: string;
  slippage_cost: string | null;
  total_cost: string;
  complete: boolean;
  slippage_complete: boolean;
  fill_count: number;
  unmatched_notional: string;
  missing_count: number;
  unassigned_count: number;
};

export type PairCycleCosts = {
  as_of: number;
  timezone: 'UTC';
  fee_rate_percent: string;
  daily: PairCostPeriod;
  weekly: PairCostPeriod;
};

export function pairCostFresh(
  report: PairCycleCosts | null | undefined,
  now: number,
  offline: boolean,
) {
  return Boolean(
    report &&
    !offline &&
    Number.isFinite(now) &&
    Number.isFinite(report.as_of) &&
    report.as_of >= 0 &&
    // The state envelope is stamped before its cost snapshot is read.
    // Allow the same one-second display clock tolerance as pair snapshots.
    now >= report.as_of - 1 &&
    now - report.as_of < 30 &&
    now >= report.daily.start &&
    now < report.daily.end &&
    now >= report.weekly.start &&
    now < report.weekly.end,
  );
}

export function pairCostPeriodView(period?: PairCostPeriod) {
  const fee = cycleAmount(period?.estimated_fee);
  const spread = cycleSignedAmount(period?.spread_cost);
  const total = cycleSignedAmount(period?.total_cost);
  const slippage = cycleSignedAmount(period?.slippage_cost);
  const unmatched = cycleAmount(period?.unmatched_notional);
  const count = (n: number | undefined) =>
    typeof n === 'number' && Number.isSafeInteger(n) && n >= 0;
  const known = ![fee, spread, total].includes('—');
  const hasUnmatched =
    unmatched !== '—' &&
    !/^0+(?:\.0+)?$/.test(period?.unmatched_notional ?? '');
  const complete = Boolean(
    period?.complete &&
    known &&
    !hasUnmatched &&
    unmatched !== '—' &&
    count(period.fill_count) &&
    period.missing_count === 0 &&
    period.unassigned_count === 0,
  );
  const notices: string[] = [];
  if (!period) notices.push('花费尚未提供，等待服务统计。');
  else {
    if (period.missing_count > 0)
      notices.push(`${period.missing_count} 项成交或归属信息待核对。`);
    if (period.unassigned_count > 0)
      notices.push(
        `${period.unassigned_count} 项成交跨统计边界或时间未知，未完整归入本期。`,
      );
    if (hasUnmatched)
      notices.push(`待配对成交金额 ${unmatched} USD1，价差尚未完整计入。`);
    if (!complete && notices.length === 0)
      notices.push('成交记录尚未完整确认，当前仅为已统计小计。');
  }
  const first = cycleUtcDate(period?.start);
  const last = period ? cycleUtcDate(period.end - 1) : null;
  const range =
    first && last
      ? first === last
        ? first
        : `${first} — ${last}`
      : '日期待确认';
  return {
    fee,
    spread,
    total,
    slippage,
    complete,
    range,
    notices,
    count: count(period?.fill_count) ? String(period!.fill_count) : '—',
    slippageComplete: Boolean(period?.slippage_complete && slippage !== '—'),
  };
}
