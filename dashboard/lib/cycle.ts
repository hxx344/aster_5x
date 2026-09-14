export type CycleConfig = {
  enabled: boolean;
  symbol: string;
  leverage: number;
  spread_notional: string;
  spread_limit_bp: string;
  min_notional: string;
  max_notional: string;
  notional_scope: 'per_side' | 'gross';
  hold_seconds: number;
  daily_volume_limit: string;
};

export type CycleDraft = Omit<CycleConfig, 'leverage' | 'hold_seconds'> & {
  leverage: string;
  hold_duration: string;
  hold_unit: 'minutes' | 'seconds';
};

export type CycleState = {
  phase?: string;
  reason?: string;
  run_id?: string;
  quantities?: Partial<Record<'LONG' | 'SHORT', string>>;
  opened_at?: number;
  close_eligible_at?: number;
  completed_cycles?: number;
  active_batch?: { stage?: string } | null;
  updated_at?: number;
  spread_bp?: string | null;
  spread_checked_at?: number | null;
  daily_volume?: CycleDailyVolume;
};

export type CycleDailyVolume = {
  utc_date: string;
  volume: string;
  trade_count: number;
  next_reset_at: number;
  limit: string;
  remaining: string | null;
  reached: boolean;
  sync_pending?: boolean;
  error?: string;
};

export type CycleTrade = {
  trade_id: string;
  order_id: string;
  client_id: string;
  symbol: string;
  position_side: string;
  side: string;
  quantity: string;
  price: string;
  notional: string;
  executed_at: number;
  time_source: string;
  utc_date: string;
  daily_volume: string;
  intent_id: string;
  phase: 'open' | 'close' | 'repair';
};

export const DEFAULT_CYCLE: CycleConfig = {
  enabled: false,
  symbol: 'XAUUSD1',
  leverage: 2,
  spread_notional: '10000',
  spread_limit_bp: '0.1',
  min_notional: '0',
  max_notional: '10000',
  notional_scope: 'per_side',
  hold_seconds: 60,
  daily_volume_limit: '0',
};

export function cycleDraft(config?: CycleConfig): CycleDraft {
  const value = { ...DEFAULT_CYCLE, ...config };
  const minutes = value.hold_seconds % 60 === 0;
  return {
    ...value,
    leverage: String(value.leverage),
    hold_duration: String(
      minutes ? value.hold_seconds / 60 : value.hold_seconds,
    ),
    hold_unit: minutes ? 'minutes' : 'seconds',
  };
}

// Compare decimal inputs as integers at a shared precision. Boundary checks
// must not accept a value above a trading limit due to Number rounding.
function decimal(value: string, label: string) {
  const text = value.trim();
  if (text.length > 128 || !/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(text))
    throw new Error(`${label}必须是有效的非负数字`);
  const [whole, fraction = ''] = text.split('.');
  return { units: BigInt(whole + fraction), places: fraction.length, text };
}

function compare(
  left: ReturnType<typeof decimal>,
  right: ReturnType<typeof decimal>,
) {
  const places = Math.max(left.places, right.places);
  const a = left.units * BigInt(10) ** BigInt(places - left.places);
  const b = right.units * BigInt(10) ** BigInt(places - right.places);
  return a < b ? -1 : a > b ? 1 : 0;
}

function amount(
  value: string,
  label: string,
  maximum: string,
  positive = false,
) {
  const parsed = decimal(value, label);
  if (parsed.text.length > 40) throw new Error(`${label}最多支持 40 个字符`);
  if (
    (positive && parsed.units === BigInt(0)) ||
    compare(parsed, decimal(maximum, label)) > 0
  )
    throw new Error(
      `${label}${positive ? '必须大于 0，且不超过 ' : '必须介于 0 至 '}${maximum}`,
    );
  return parsed;
}

export function cycleHoldSeconds(
  value: string,
  unit: CycleDraft['hold_unit'],
): number {
  if (unit !== 'minutes' && unit !== 'seconds')
    throw new Error('请选择分钟或秒');
  const parsed = decimal(value, '持仓时间');
  const units = parsed.units * BigInt(unit === 'minutes' ? 60 : 1);
  const divisor = BigInt(10) ** BigInt(parsed.places);
  if (
    units === BigInt(0) ||
    units % divisor !== BigInt(0) ||
    units / divisor > BigInt(604800)
  )
    throw new Error('持仓时间必须换算为 1 至 604800 的整数秒（最多 7 天）');
  return Number(units / divisor);
}

export function parseCycleDraft(draft: CycleDraft): CycleConfig {
  if (!['XAUUSD1', 'SPCXUSD1', 'CLUSD1'].includes(draft.symbol))
    throw new Error('请选择支持的循环品种');
  if (
    !/^\d+$/.test(draft.leverage) ||
    Number(draft.leverage) < 1 ||
    Number(draft.leverage) > 125
  )
    throw new Error('循环杠杆必须是 1 至 125 的整数');
  if (!['per_side', 'gross'].includes(draft.notional_scope))
    throw new Error('请选择单边或多空合计的名义价值口径');
  const spread = amount(draft.spread_notional, '深度参考金额', '1000000', true);
  const bp = amount(draft.spread_limit_bp, '价差上限（bp）', '100');
  const min = amount(draft.min_notional, '名义价值下限', '1000000');
  const max = amount(draft.max_notional, '名义价值上限', '1000000', true);
  const dailyLimit = amount(
    draft.daily_volume_limit,
    '每日成交额度',
    '1000000000000',
  );
  if (compare(min, max) > 0) throw new Error('名义价值下限不能大于上限');
  return {
    enabled: draft.enabled,
    symbol: draft.symbol,
    leverage: Number(draft.leverage),
    spread_notional: spread.text,
    spread_limit_bp: bp.text,
    min_notional: min.text,
    max_notional: max.text,
    notional_scope: draft.notional_scope,
    hold_seconds: cycleHoldSeconds(draft.hold_duration, draft.hold_unit),
    daily_volume_limit: dailyLimit.text,
  };
}

const PHASES: Record<string, { label: string; reason: string }> = {
  disabled: { label: '未开启', reason: '保存启用设置后，可单独启动本账户循环' },
  paused: { label: '已暂停', reason: '已有仓位与持仓计时保留，启动账户后继续' },
  attention: { label: '需要处理', reason: '请核对当前批次，确认成交后再继续' },
  waiting_open: {
    label: '等待开仓',
    reason: '等待深度价差、可用额度与风险条件满足',
  },
  opening: { label: '多空开仓中', reason: '正在提交并核对本轮多空开仓' },
  reconciling: { label: '核对中', reason: '正在核对成交，暂不开始下一步' },
  holding: { label: '持仓计时', reason: '多空开仓已确认，等待最短持仓时间' },
  waiting_close: {
    label: '等待平仓',
    reason: '持仓时间已满足，等待深度价差达标后平仓',
  },
  closing: { label: '多空平仓中', reason: '正在提交并核对本轮多空平仓' },
  daily_limit: {
    label: '等待次日额度',
    reason: '当日额度已用完或不足开启下一轮，下一 UTC 日条件满足后自动恢复新增',
  },
};

export function cycleStatus(
  state: CycleState | undefined,
  enabled: boolean,
  accountEnabled: boolean,
) {
  let phase =
    state?.phase ||
    (!enabled ? 'disabled' : accountEnabled ? 'waiting_open' : 'paused');
  if (phase === 'daily_limit' && (!enabled || !accountEnabled)) {
    phase = enabled ? 'paused' : 'disabled';
    return {
      phase,
      label: PHASES[phase].label,
      reason: enabled
        ? '账户已手动暂停，UTC 换日后仍需手动启动'
        : PHASES.disabled.reason,
    };
  }
  const fallback = PHASES[phase] || {
    label: '等待状态',
    reason: '等待更新循环状态',
  };
  return {
    phase,
    label: fallback.label,
    reason: state?.reason || fallback.reason,
  };
}

export function cycleHasPosition(state?: CycleState): boolean {
  return ['LONG', 'SHORT'].some((side) => {
    const quantity = state?.quantities?.[side as 'LONG' | 'SHORT'];
    return (
      quantity !== undefined &&
      !/^[+-]?(?:0+(?:\.0*)?|\.0+)(?:[eE][+-]?\d+)?$/.test(quantity)
    );
  });
}

export function cycleCountdown(
  state: CycleState | undefined,
  now: number,
  stale = false,
): string {
  if (!cycleHasPosition(state)) return '—';
  if (
    !state?.close_eligible_at ||
    !Number.isFinite(state.close_eligible_at) ||
    !Number.isFinite(now)
  )
    return '等待开仓确认';
  if (stale) return '数据过期，等待刷新';
  const remaining = Math.max(0, Math.ceil(state.close_eligible_at - now));
  if (!remaining) return '时间已满足，仍需价差达标';
  return `${Math.floor(remaining / 60)} 分 ${remaining % 60} 秒`;
}

export function cycleSpreadView(
  state: CycleState | undefined,
  now: number,
  dataStale = false,
) {
  const value = state?.spread_bp;
  const timestamp = state?.spread_checked_at;
  const knownValue =
    value !== undefined &&
    value !== null &&
    value.trim() !== '' &&
    Number.isFinite(Number(value)) &&
    Number(value) >= 0;
  const knownTime =
    typeof timestamp === 'number' &&
    timestamp > 0 &&
    Number.isFinite(timestamp);
  const stale =
    dataStale ||
    !knownTime ||
    !Number.isFinite(now) ||
    now < timestamp - 1 ||
    now - timestamp > 3;
  return {
    value: knownValue
      ? Number(value).toLocaleString('en-US', { maximumSignificantDigits: 5 })
      : '—',
    timestamp: knownTime ? timestamp : undefined,
    notice: !knownValue
      ? '等待检测'
      : !knownTime
        ? '检测时间未知'
        : stale
          ? '已过期'
          : '最近检测',
    stale: knownValue && stale,
  };
}
