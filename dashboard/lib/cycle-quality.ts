export type CycleExecutionEstimate = {
  status: 'available' | 'unavailable';
  sampled_at: number | null;
  checked_at: number | null;
  quantity: string | null;
  buy_vwap: string | null;
  sell_vwap: string | null;
  spread_bp: string | null;
};

export type CycleExecutionQuality = {
  version: 1;
  intent_id: string;
  symbol: string;
  phase: 'open' | 'close';
  quantity: string;
  created_at: number;
  updated_at: number;
  trigger: {
    source: 'bbo' | 'depth' | 'poll' | 'unknown';
    received_at: number | null;
  };
  trigger_estimate: CycleExecutionEstimate | null;
  final_estimate: CycleExecutionEstimate | null;
  timing: {
    request_started_at: number | null;
    response_received_at: number | null;
    trigger_to_request_ms: number | null;
    final_check_to_request_ms: number | null;
    request_to_response_ms: number | null;
    request_status: 'returned' | 'failed' | 'not_sent' | 'unknown';
    database?: {
      lock_wait_ms: number | null;
      connection_ms: number | null;
      connections: number;
    };
    transport?: {
      guard_ms?: number | null;
      budget_ms?: number | null;
      signing_ms?: number | null;
      before_http_ms?: number | null;
      http_ms?: number | null;
      response_decode_ms?: number | null;
      http_started_at?: number | null;
      http_finished_at?: number | null;
    };
    pre_submit?: {
      queue_ms: number | null;
      initial_account_ms: number | null;
      planning_ms: number | null;
      final_account_ms: number | null;
      final_check_ms: number | null;
      persist_ms: number | null;
      other_ms: number | null;
    };
  };
  actual: {
    status:
      | 'filled'
      | 'partial'
      | 'pending'
      | 'rejected'
      | 'not_sent'
      | 'unknown';
    confirmed: boolean;
    quantity: string | null;
    buy_vwap: string | null;
    sell_vwap: string | null;
    spread_bp: string | null;
    buy_quantity: string | null;
    sell_quantity: string | null;
    buy_status: string | null;
    sell_status: string | null;
    repairs_present: boolean;
  };
};

function text(value: unknown, fallback = '—'): string {
  return typeof value === 'string' && value.trim() ? value : fallback;
}

function mappedText(
  labels: Record<string, string>,
  value: unknown,
  fallback: string,
): string {
  return typeof value === 'string' && Object.hasOwn(labels, value)
    ? labels[value]
    : fallback;
}

// Preserve the service's decimal strings, including favorable negative spread.
// Quantity equality must not round small residuals through Number.
function decimal(value: unknown, signed = false, zero = true): string {
  if (typeof value !== 'string' || !/^-?(?:\d+(?:\.\d*)?|\.\d+)$/.test(value))
    return '—';
  if (!signed && value.startsWith('-')) return '—';
  if (!zero && /^-?(?:0+(?:\.0*)?|\.0+)$/.test(value)) return '—';
  return value;
}

function sameQuantity(left: unknown, right: unknown): boolean {
  if (
    decimal(left, false, false) === '—' ||
    decimal(right, false, false) === '—'
  )
    return false;
  const normalized = (value: string) => {
    const [whole, fraction = ''] = value.split('.');
    return `${whole.replace(/^0+/, '') || '0'}.${fraction.replace(/0+$/, '')}`;
  };
  return normalized(left as string) === normalized(right as string);
}

function validTime(value: unknown): value is number {
  return (
    typeof value === 'number' &&
    Number.isFinite(value) &&
    value >= 0 &&
    Number.isFinite(new Date(value * 1000).getTime())
  );
}

export function cycleQualityTime(value: number | null | undefined): string {
  return validTime(value)
    ? `${new Date(value * 1000).toISOString().replace('T', ' ').replace('Z', ' UTC')}`
    : '—';
}

function duration(value: unknown, from: unknown, to: unknown): string {
  return typeof value === 'number' &&
    Number.isFinite(value) &&
    value >= 0 &&
    validTime(from) &&
    validTime(to) &&
    to >= from
    ? String(value)
    : '—';
}

function estimateView(
  estimate: CycleExecutionEstimate | null | undefined,
  quantity: string,
) {
  const same = sameQuantity(estimate?.quantity, quantity);
  const spread = decimal(estimate?.spread_bp, true);
  const buy = decimal(estimate?.buy_vwap, false, false);
  const sell = decimal(estimate?.sell_vwap, false, false);
  const available =
    estimate?.status === 'available' &&
    same &&
    spread !== '—' &&
    buy !== '—' &&
    sell !== '—';
  const status = !estimate
    ? '未提供'
    : estimate.status === 'unavailable'
      ? '估计不可用'
      : !same
        ? '数量缺失或不一致'
        : !available
          ? '估计数据不完整'
          : '同数量估计';
  return {
    available,
    status,
    spread: available ? spread : '—',
    quantity: decimal(estimate?.quantity, false, false),
    buy,
    sell,
    sampledAt: cycleQualityTime(estimate?.sampled_at),
    checkedAt: cycleQualityTime(estimate?.checked_at),
  };
}

const ACTUAL_STATUS: Record<string, string> = {
  filled: '双边已确认',
  partial: '部分成交',
  pending: '待确认',
  rejected: '已拒绝',
  not_sent: '未发单',
  unknown: '未确认',
};
const ORDER_STATUS: Record<string, string> = {
  FILLED: '全部成交',
  PARTIALLY_FILLED: '部分成交',
  NEW: '已接受，待成交',
  CANCELED: '已撤销',
  EXPIRED: '已过期',
  EXPIRED_IN_MATCH: '撮合时失效',
  REJECTED: '已拒绝',
};
const REQUEST_STATUS: Record<string, string> = {
  returned: '调用已返回',
  failed: '调用失败，响应未确认',
  not_sent: '未发单',
  unknown: '未记录',
};

const PRE_SUBMIT_STAGES = [
  ['queue_ms', '触发→账户任务开始'],
  ['initial_account_ms', '账户热数据读取'],
  ['planning_ms', '首轮规划与盘口'],
  ['final_account_ms', '提交前账户复核'],
  ['final_check_ms', '最终盘口与风控检查'],
  ['persist_ms', '订单记录写入'],
  ['other_ms', '其他本地准备'],
] as const;

export function cycleExecutionQualityView(
  quality?: CycleExecutionQuality | null,
) {
  if (!quality || quality.version !== 1) return null;
  const quantity = decimal(quality.quantity, false, false);
  const actual = quality.actual;
  const spread = decimal(actual?.spread_bp, true);
  const buy = decimal(actual?.buy_vwap, false, false);
  const sell = decimal(actual?.sell_vwap, false, false);
  const same =
    sameQuantity(actual?.quantity, quantity) &&
    sameQuantity(actual?.buy_quantity, quantity) &&
    sameQuantity(actual?.sell_quantity, quantity);
  const filled =
    actual?.status === 'filled' &&
    actual.confirmed === true &&
    actual.buy_status === 'FILLED' &&
    actual.sell_status === 'FILLED';
  const complete =
    filled && same && spread !== '—' && buy !== '—' && sell !== '—';
  const actualStatus =
    actual?.status !== 'filled'
      ? mappedText(ACTUAL_STATUS, actual?.status, '成交状态未记录')
      : !filled
        ? '成交尚未完整确认'
        : !same
          ? '数量缺失或不一致'
          : !complete
            ? '成交数据不完整'
            : ACTUAL_STATUS.filled;
  const triggerSource = quality.trigger?.source;
  const wsTrigger = triggerSource === 'bbo' || triggerSource === 'depth';
  const knownTrigger = wsTrigger || triggerSource === 'poll';
  const timing = quality.timing;
  return {
    intentId: text(quality.intent_id),
    symbol: text(quality.symbol, '品种未记录'),
    phase:
      quality.phase === 'open'
        ? '开仓'
        : quality.phase === 'close'
          ? '平仓'
          : '阶段未记录',
    quantity,
    updatedAt: cycleQualityTime(quality.updated_at),
    createdAt: cycleQualityTime(quality.created_at),
    trigger: estimateView(quality.trigger_estimate, quantity),
    final: estimateView(quality.final_estimate, quantity),
    actual: {
      complete,
      status: actualStatus,
      spread: complete ? spread : '—',
      buy: filled && same ? buy : '—',
      sell: filled && same ? sell : '—',
      quantity: filled && same ? decimal(actual?.quantity, false, false) : '—',
      buyQuantity: decimal(actual?.buy_quantity),
      sellQuantity: decimal(actual?.sell_quantity),
      buyStatus: mappedText(
        ORDER_STATUS,
        actual?.buy_status,
        text(actual?.buy_status, '未确认'),
      ),
      sellStatus: mappedText(
        ORDER_STATUS,
        actual?.sell_status,
        text(actual?.sell_status, '未确认'),
      ),
      repairs: actual?.repairs_present === true,
    },
    timing: [
      {
        label:
          triggerSource === 'poll'
            ? '轮询唤醒 → 请求开始'
            : 'WS 接收 → 请求开始',
        value: knownTrigger
          ? duration(
              timing?.trigger_to_request_ms,
              quality.trigger?.received_at,
              timing?.request_started_at,
            )
          : '—',
      },
      {
        label: '最终深度检查 → 请求开始',
        value: duration(
          timing?.final_check_to_request_ms,
          quality.final_estimate?.checked_at,
          timing?.request_started_at,
        ),
      },
      {
        label: '请求开始 → 收到响应',
        value:
          timing?.request_status === 'returned'
            ? duration(
                timing.request_to_response_ms,
                timing.request_started_at,
                timing.response_received_at,
              )
            : '—',
      },
    ],
    preSubmit: PRE_SUBMIT_STAGES.map(([key, label]) => {
      const value = timing?.pre_submit?.[key];
      return {
        key,
        label,
        value:
          typeof value === 'number' && Number.isFinite(value) && value >= 0
            ? String(value)
            : '—',
      };
    }),
    database: [
      ['lock_wait_ms', '等待账本访问锁'],
      ['connection_ms', '建立数据库连接'],
    ].map(([key, label]) => {
      const value = timing?.database?.[key as 'lock_wait_ms' | 'connection_ms'];
      return { label, value: observedDuration(value) };
    }),
    transport: [
      ['guard_ms', '发单前本地校验'],
      ['budget_ms', '请求额度检查'],
      ['signing_ms', '签名与请求编码'],
      ['http_ms', 'HTTP 调用（含连接处理）'],
      ['response_decode_ms', '解析响应'],
    ].map(([key, label]) => ({
      label,
      value: observedDuration(
        timing?.transport?.[
          key as keyof NonNullable<CycleExecutionQuality['timing']['transport']>
        ],
      ),
    })),
    beforeHttp: observedDuration(timing?.transport?.before_http_ms),
    triggerSource:
      triggerSource === 'bbo'
        ? 'WS 最优盘口'
        : triggerSource === 'depth'
          ? 'WS 深度'
          : triggerSource === 'poll'
            ? '轮询唤醒'
            : '触发来源未记录',
    triggerReceivedAt: cycleQualityTime(quality.trigger?.received_at),
    requestStartedAt: cycleQualityTime(timing?.request_started_at),
    responseReceivedAt:
      timing?.request_status === 'returned'
        ? cycleQualityTime(timing.response_received_at)
        : '—',
    requestStatus: mappedText(REQUEST_STATUS, timing?.request_status, '未记录'),
  };
}

function observedDuration(value: unknown) {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0
    ? String(value)
    : '—';
}
