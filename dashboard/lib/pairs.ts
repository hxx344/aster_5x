import type { Account } from './desk-types';
import {
  cycleDraft,
  parseCycleDraft,
  type CycleConfig,
  type CycleDraft,
} from './cycle.ts';
import {
  marginLimitFromPercent,
  percentFromMarginLimit,
  parseMinimumLeverage,
} from './policy.ts';

export type PairSnapshot = NonNullable<Account['snapshot']>;
export type PairApiNotice = {
  kind: 'budget' | 'cooldown' | 'rate_limit';
  text: string;
};
export type PairTransfer = {
  request_id: string;
  source: 'long' | 'short';
  destination: 'long' | 'short';
  amount: string;
  created_at: number;
  status: string;
  confirmed_at?: number;
  acknowledged_at?: number;
  refreshed_at?: number;
  transaction_id?: string;
};
export type PairMarginState = {
  enabled?: boolean;
  status?: string;
  reason?: string;
  api_notice?: PairApiNotice | null;
  blocks_trading?: boolean;
  checked_at?: number;
  pending?: PairTransfer | null;
  last_transfer?: PairTransfer | null;
  plan?: Partial<PairTransfer> | null;
  next_check_at?: number;
  cooldown_until?: number;
};
export type Pair = {
  id: string;
  name: string;
  long_account_id: string;
  short_account_id: string;
  symbol: 'XAUUSD1';
  enabled: boolean;
  revision?: number;
  pause_reason?: string;
  ordinary: {
    enabled: boolean;
    threshold: string;
    order_notional: string;
    min_open_leverage: number;
    margin_limit: string;
    spread_limit: string;
  };
  cycle: CycleConfig;
  margin: {
    enabled: boolean;
    master_env_prefix: string;
    check_interval_seconds: number;
    threshold: string;
    min_transfer: string;
    max_transfer: string;
    buffer_ratio: string;
    cooldown_seconds: number;
  };
  state?: {
    phase?: string;
    reason?: string;
    api_notice?: PairApiNotice | null;
    updated_at?: number;
    snapshots?: { long?: PairSnapshot | null; short?: PairSnapshot | null };
    owned?: Partial<Record<'LONG' | 'SHORT', string>>;
    progress?: {
      phase?: string;
      baseline?: Partial<Record<'LONG' | 'SHORT', string>>;
      quantities?: Partial<Record<'LONG' | 'SHORT', string>>;
      leverage?: number;
      config?: CycleConfig;
      opened_at?: number;
      close_eligible_at?: number;
      completed_cycles?: number;
    } | null;
    pending?: Record<string, unknown> | null;
    recovery_watch?: { batches: { id: string }[] } | null;
    margin?: PairMarginState;
    last_batch?: {
      id: string;
      kind: 'cycle' | 'ordinary';
      phase: 'open' | 'close';
      quantity: string;
      completed: boolean;
      resolution?: 'manual_skip';
      positions_verified?: boolean;
      at: number;
    };
    daily_volume?: Record<string, { long?: string; short?: string }>;
    volume_unknown?: boolean;
  };
};
export type PairDraft = {
  id: string;
  name: string;
  long_account_id: string;
  short_account_id: string;
  mode: 'monitor' | 'ordinary' | 'cycle';
  threshold: string;
  order_notional: string;
  min_open_leverage: string;
  margin_percent: string;
  spread_limit: string;
  cycle: CycleDraft;
  margin_enabled: boolean;
  master_env_prefix: string;
  check_interval_seconds: string;
  balance_threshold: string;
  min_transfer: string;
  max_transfer: string;
  buffer_percent: string;
  cooldown_seconds: string;
};

export function pairDraft(pair?: Pair): PairDraft {
  return {
    id: pair?.id ?? '',
    name: pair?.name ?? '',
    long_account_id: pair?.long_account_id ?? '',
    short_account_id: pair?.short_account_id ?? '',
    mode: pair?.ordinary.enabled
      ? 'ordinary'
      : pair?.cycle.enabled
        ? 'cycle'
        : 'monitor',
    threshold: pair?.ordinary.threshold ?? '10000',
    order_notional: pair?.ordinary.order_notional ?? '1000',
    min_open_leverage: String(pair?.ordinary.min_open_leverage ?? 5),
    margin_percent: percentFromMarginLimit(
      pair?.ordinary.margin_limit ?? '0.5',
    ),
    spread_limit: pair?.ordinary.spread_limit ?? '0.0005',
    cycle: cycleDraft(pair?.cycle),
    margin_enabled: pair?.margin.enabled ?? false,
    master_env_prefix: pair?.margin.master_env_prefix ?? '',
    check_interval_seconds: String(pair?.margin.check_interval_seconds ?? 5),
    balance_threshold: pair?.margin.threshold ?? '10',
    min_transfer: pair?.margin.min_transfer ?? '1',
    max_transfer: pair?.margin.max_transfer ?? '1000',
    buffer_percent: percentFromMarginLimit(pair?.margin.buffer_ratio ?? '0.05'),
    cooldown_seconds: String(pair?.margin.cooldown_seconds ?? 30),
  };
}

function amount(
  value: string,
  label: string,
  allowZero = false,
  maxLength = 40,
): string {
  const text = value.trim();
  if (text.length > maxLength)
    throw new Error(`${label}最多支持 ${maxLength} 个字符`);
  const parsed = quantityUnits(text);
  if (
    !/^\d/.test(text) ||
    !parsed ||
    (!allowZero && parsed.units === BigInt(0))
  )
    throw new Error(
      `${label}必须是${allowZero ? '非负' : '大于零的'}金额或数值`,
    );
  return text;
}

// Inputs have already passed amount(); align their decimal places exactly.
function compareAmounts(left: string, right: string): number {
  const a = quantityUnits(left)!;
  const b = quantityUnits(right)!;
  const scale = Math.max(a.scale, b.scale);
  const leftUnits = a.units * BigInt(10) ** BigInt(scale - a.scale);
  const rightUnits = b.units * BigInt(10) ** BigInt(scale - b.scale);
  return leftUnits < rightUnits ? -1 : leftUnits > rightUnits ? 1 : 0;
}

function seconds(value: string, label: string, maximum: number): number {
  if (
    !/^\d+$/.test(value) ||
    !Number.isSafeInteger(Number(value)) ||
    Number(value) < 1 ||
    Number(value) > maximum
  )
    throw new Error(`${label}必须是 1 至 ${maximum} 的整数秒`);
  return Number(value);
}

export function availablePairAccounts(
  accounts: Account[],
  pairs: Pair[],
  currentId?: string,
): Account[] {
  const occupied = new Set(
    pairs
      .filter((pair) => pair.id !== currentId)
      .flatMap((pair) => [pair.long_account_id, pair.short_account_id]),
  );
  return accounts.filter(
    (account) =>
      !occupied.has(account.id) &&
      (!account.pair_id || account.pair_id === currentId),
  );
}

export function parsePairDraft(
  draft: PairDraft,
  accounts: Account[],
  pairs: Pair[],
  currentId?: string,
) {
  if (!/^[a-z0-9_-]{1,32}$/.test(draft.id))
    throw new Error('组标识只允许 1–32 位小写字母、数字、下划线或短横线');
  const name = draft.name.trim();
  if (!name || name.length > 50) throw new Error('组名称须为 1–50 个字符');
  const available = availablePairAccounts(accounts, pairs, currentId);
  const long = available.find(
    (account) => account.id === draft.long_account_id,
  );
  const short = available.find(
    (account) => account.id === draft.short_account_id,
  );
  if (!long || !short || long.id === short.id)
    throw new Error('请选择两个不同且未被其他组占用的子账户');
  if (long.mode !== short.mode || !['live', 'paper'].includes(long.mode))
    throw new Error('两侧必须使用相同的实盘或模拟环境');
  if (!currentId && (long.enabled || short.enabled))
    throw new Error('请先暂停两侧账户再创建配对组');
  if (!['monitor', 'ordinary', 'cycle'].includes(draft.mode))
    throw new Error('请选择配对执行模式');
  const prefix = draft.master_env_prefix.trim();
  if (prefix && !/^[A-Z][A-Z0-9_]{1,40}$/.test(prefix))
    throw new Error('主账户凭据前缀格式无效');
  if (long.mode === 'live' && !prefix)
    throw new Error(
      '实盘配对组须填写独立主账户凭据环境变量前缀，用于核验两侧共同归属',
    );
  if (
    long.mode === 'live' &&
    [long.env_prefix, short.env_prefix].includes(prefix)
  )
    throw new Error('主账户凭据前缀必须独立，不能使用任一子账户凭据前缀');
  const minTransfer = amount(draft.min_transfer, '最小划转金额');
  const maxTransfer = amount(draft.max_transfer, '最大划转金额');
  if (compareAmounts(minTransfer, '0.00000001') < 0)
    throw new Error('最小划转金额至少为 0.00000001 USD1');
  if (compareAmounts(minTransfer, maxTransfer) > 0)
    throw new Error('最小划转金额不能大于最大划转金额');
  if (compareAmounts(maxTransfer, '1000000000') > 0)
    throw new Error('最大划转金额不能超过 1000000000 USD1');
  const threshold = amount(draft.threshold, '公共额度门槛', true);
  const notional = amount(draft.order_notional, '每侧开仓金额');
  const spread = amount(draft.spread_limit, '价差比例', false, 128);
  const balanceThreshold = amount(
    draft.balance_threshold,
    '可用余额差额门槛',
    true,
  );
  if (compareAmounts(threshold, '1000000000') > 0)
    throw new Error('公共额度门槛不能超过 1000000000 USD1');
  if (
    compareAmounts(notional, '500') < 0 ||
    compareAmounts(notional, '1000000') > 0
  )
    throw new Error('每侧开仓金额须为 500–1000000 USD1');
  if (compareAmounts(spread, '0.0005') > 0)
    throw new Error('普通开仓价差比例不能超过 0.0005');
  if (compareAmounts(balanceThreshold, '1000000000') > 0)
    throw new Error('可用余额差额门槛不能超过 1000000000 USD1');
  const buffer = amount(draft.buffer_percent, '保留缓冲比例', true, 128);
  if (compareAmounts(buffer, '100') >= 0)
    throw new Error('保留缓冲比例须小于 100%');
  return {
    id: draft.id,
    name,
    long_account_id: long.id,
    short_account_id: short.id,
    symbol: 'XAUUSD1' as const,
    ordinary: {
      enabled: draft.mode === 'ordinary',
      threshold,
      order_notional: notional,
      min_open_leverage: parseMinimumLeverage(draft.min_open_leverage),
      margin_limit: marginLimitFromPercent(draft.margin_percent),
      spread_limit: spread,
    },
    cycle: parseCycleDraft({
      ...draft.cycle,
      symbol: 'XAUUSD1',
      enabled: draft.mode === 'cycle',
    }),
    margin: {
      enabled: draft.margin_enabled,
      master_env_prefix: prefix,
      check_interval_seconds: seconds(
        draft.check_interval_seconds,
        '平衡检查间隔',
        3600,
      ),
      threshold: balanceThreshold,
      min_transfer: minTransfer,
      max_transfer: maxTransfer,
      buffer_ratio:
        compareAmounts(buffer, '0') === 0
          ? '0'
          : marginLimitFromPercent(buffer),
      cooldown_seconds: seconds(draft.cooldown_seconds, '划转冷却', 86400),
    },
  };
}

export function pairConfigurationChanges(
  config: ReturnType<typeof parsePairDraft>,
) {
  const { name, ordinary, cycle, margin } = config;
  return { name, ordinary, cycle, margin };
}

export function pairDataFresh(
  timestamp: number | undefined,
  now: number,
  offline = false,
): boolean {
  return (
    !offline &&
    timestamp !== undefined &&
    Number.isFinite(timestamp) &&
    timestamp > 0 &&
    Number.isFinite(now) &&
    now >= timestamp - 1 &&
    now - timestamp < 8
  );
}
export function pairSnapshotStatus(
  snapshot: PairSnapshot | null | undefined,
  now: number,
  offline = false,
) {
  if (!snapshot) return '等待账户数据';
  if (offline) return '连接中断 · 最近记录';
  return pairDataFresh(snapshot.timestamp, now)
    ? '最近快照'
    : '数据过期 · 等待刷新';
}
export function pairHasPending(pair: Pair): boolean {
  return (
    Boolean(pair.state?.pending && Object.keys(pair.state.pending).length) ||
    Boolean(pair.state?.margin?.pending) ||
    pair.state?.margin?.status === 'unknown'
  );
}
export function pairStartRecoveryBlock(pair: Pair): string {
  const margin = pair.state?.margin;
  if (
    margin?.pending ||
    ['submitting', 'accepted', 'acknowledged', 'unknown'].includes(
      margin?.status ?? '',
    )
  )
    return '划转结果仍待核对，完成后才能启动';
  if (
    Object.values(pair.state?.progress?.quantities ?? {}).some((value) => {
      const quantity = typeof value === 'string' ? quantityUnits(value) : null;
      return !quantity || quantity.units !== BigInt(0);
    })
  )
    return '本轮循环新增仓位仍需恢复，完成后才能启动';
  const pending = pair.state?.pending;
  if (
    pending &&
    Object.keys(pending).length &&
    (pending.kind !== 'ordinary' || pending.phase !== 'open')
  )
    return '循环、减仓或杠杆批次仍待核对，完成后才能启动';
  // An ordinary opening may request reconciliation at start. Only the server
  // can validate receipts and current positions, then authorize activation.
  return '';
}
export function pairConfigurationLock(pair: Pair): string {
  if (pair.enabled) return '暂停配对组后可修改设置。';
  if (pairHasPending(pair)) return '订单或划转结果待核对，暂不能修改设置。';
  if (
    Object.values(pair.state?.progress?.quantities ?? {}).some(
      (value) => Number(value) !== 0,
    )
  )
    return '本轮循环减回基线后可修改设置。';
  return '';
}
export function pairDeletionBlock(
  pair: Pair,
  now: number,
  offline = false,
): string {
  if (offline) return '连接恢复后可删除配对组';
  if (pair.enabled) return '请先暂停配对组';
  if (pairHasPending(pair)) return '仍有订单或划转等待确认';
  for (const side of ['long', 'short'] as const) {
    const snapshot = pair.state?.snapshots?.[side];
    // Paused groups may have no hot snapshot. DELETE re-reads both accounts
    // under the group lock; stale UI data must not prevent that verification.
    if (
      snapshot &&
      pairDataFresh(snapshot.timestamp, now) &&
      (!Array.isArray(snapshot.positions) ||
        snapshot.positions.some(
          (position) =>
            !Number.isFinite(Number(position.qty)) ||
            Number(position.qty) !== 0,
        ))
    )
      return '两侧所有仓位完全减回后才能删除组';
  }
  return '';
}

// Keep small quantity differences exact; missing/invalid source data remains unknown.
function quantityUnits(value: string): { units: bigint; scale: number } | null {
  if (!/^[+-]?\d+(?:\.\d+)?$/.test(value) || value.length > 128) return null;
  const [whole, decimal = ''] = value.replace(/^[+-]/, '').split('.');
  return { units: BigInt(whole + decimal), scale: decimal.length };
}
export function pairNetQuantity(
  snapshots:
    | { long?: PairSnapshot | null; short?: PairSnapshot | null }
    | undefined,
): string | null {
  if (
    !snapshots?.long ||
    !snapshots.short ||
    !Array.isArray(snapshots.long.positions) ||
    !Array.isArray(snapshots.short.positions)
  )
    return null;
  const values: { units: bigint; scale: number; sign: bigint }[] = [];
  for (const side of ['long', 'short'] as const) {
    for (const position of snapshots[side]!.positions) {
      if (position.symbol !== 'XAUUSD1') continue;
      const parsed = quantityUnits(position.qty);
      if (!parsed || !['LONG', 'SHORT'].includes(position.side)) return null;
      values.push({
        ...parsed,
        sign: position.side === 'LONG' ? BigInt(1) : BigInt(-1),
      });
    }
  }
  const scale = Math.max(0, ...values.map((value) => value.scale));
  const total = values.reduce(
    (sum, value) =>
      sum +
      value.units * BigInt(10) ** BigInt(scale - value.scale) * value.sign,
    BigInt(0),
  );
  const negative = total < BigInt(0);
  const digits = (negative ? -total : total)
    .toString()
    .padStart(scale + 1, '0');
  const result = scale
    ? `${digits.slice(0, -scale)}.${digits.slice(-scale)}`.replace(/\.?0+$/, '')
    : digits;
  return `${negative ? '-' : ''}${result || '0'}`;
}

export const PAIR_PHASES: Record<string, string> = {
  paused: '已暂停',
  disabled: '未开启',
  idle: '等待执行',
  waiting: '等待条件',
  waiting_open: '等待共同开仓',
  opening: '两侧开仓核对',
  holding: '持仓计时',
  waiting_close: '等待减回',
  closing: '减回本轮基线',
  reconciling: '订单结果核对中',
  repairing: '风险恢复中',
  repair: '风险恢复中',
  attention: '需要处理',
  error: '执行异常',
  blocked: '条件阻止执行',
  daily_limit: '等待日额度',
  monitoring: '保证金管理中',
  unknown: '结果未知 · 继续查询',
  accepted: '请求已受理 · 等待确认',
  cooldown: '划转冷却中',
  confirmed: '两侧划转流水已核实',
  acknowledged: '交易所已确认 · 等待余额刷新',
  refreshed: '交易所回执确认，余额已刷新',
  paper_confirmed: '模拟划转已确认',
  rejected: '划转被拒绝',
  margin_wait: '保证金检查阻止新增',
  submitting: '并行提交两侧市价单',
  leverage: '共同升杠杆核对',
};
export function pairPhaseLabel(phase?: string): string {
  return phase ? (PAIR_PHASES[phase] ?? phase) : '等待状态';
}

export function pairTransferStatus(transfer: PairTransfer): string {
  if (transfer.status === 'submitting') return '划转请求提交中 · 等待回执';
  if (
    transfer.status === 'acknowledged' &&
    typeof transfer.refreshed_at === 'number' &&
    Number.isFinite(transfer.refreshed_at) &&
    transfer.refreshed_at > 0
  )
    return PAIR_PHASES.refreshed;
  return pairPhaseLabel(transfer.status);
}

export function pairMarginStatus(margin?: PairMarginState): string {
  if (margin?.status === 'submitting') return '划转请求提交中 · 等待回执';
  if (
    margin?.status === 'acknowledged' &&
    !margin.pending &&
    margin.last_transfer
  )
    return pairTransferStatus(margin.last_transfer);
  return pairPhaseLabel(margin?.status);
}
