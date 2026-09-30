import type { Pair } from './pairs';

type Quantities = Record<'LONG' | 'SHORT', string>;
export type PairOrderRecoveryReview = {
  status: 'review';
  token: string;
  pair_id: string;
  batch_id: string;
  created_at: number;
  checked_at: number;
  before: Quantities;
  actual: Quantities;
  leverage: number;
  orders: {
    side: 'LONG' | 'SHORT';
    client_order_id: string;
    result: 'not_found';
  }[];
  message: string;
};
type ReceiptsFound = { status: 'receipts_found'; message: string };

export type PairOrderRecoveryCheck = {
  status: 'checked';
  pair_id: string;
  batch_id: string;
  checked_at: number;
  completed: boolean;
  message: string;
  archive_available: boolean;
  archive_reason: string;
  orders: {
    side: 'LONG' | 'SHORT';
    client_order_id: string;
    status: string;
    executed_qty: string | null;
    error: string;
  }[];
};

export function parsePairOrderRecoveryCheck(
  data: unknown,
  pair: Pair,
): PairOrderRecoveryCheck {
  if (!record(data)) throw new Error('核对响应不完整，请重新核对');
  if (data.pair_id !== pair.id || data.batch_id !== pair.state?.pending?.id)
    throw new Error('核对响应与请求的配对组或批次不符，请重新核对');
  if (
    data.status !== 'checked' ||
    typeof data.message !== 'string' ||
    typeof data.checked_at !== 'number' ||
    !Number.isFinite(data.checked_at) ||
    data.checked_at <= 0 ||
    typeof data.completed !== 'boolean' ||
    typeof data.archive_available !== 'boolean' ||
    typeof data.archive_reason !== 'string' ||
    (data.completed && data.archive_available) ||
    !Array.isArray(data.orders) ||
    data.orders.length < 2 ||
    !data.orders.every(
      (order) =>
        record(order) &&
        (order.side === 'LONG' || order.side === 'SHORT') &&
        typeof order.client_order_id === 'string' &&
        Boolean(order.client_order_id) &&
        typeof order.status === 'string' &&
        [
          'UNKNOWN',
          'NEW',
          'PARTIALLY_FILLED',
          'PENDING_CANCEL',
          'FILLED',
          'CANCELED',
          'REJECTED',
          'EXPIRED',
          'EXPIRED_IN_MATCH',
        ].includes(order.status) &&
        (order.executed_qty === null ||
          (typeof order.executed_qty === 'string' &&
            /^\d+(?:\.\d+)?$/.test(order.executed_qty))) &&
        typeof order.error === 'string',
    ) ||
    new Set(data.orders.map((order) => order.side)).size !== 2 ||
    new Set(data.orders.map((order) => order.client_order_id)).size !==
      data.orders.length
  )
    throw new Error('核对响应不完整，请重新核对');
  // A check result can never supply an archive token, even if the server sends one.
  return {
    status: 'checked',
    pair_id: data.pair_id as string,
    batch_id: data.batch_id as string,
    checked_at: data.checked_at,
    completed: data.completed,
    message: data.message,
    archive_available: data.archive_available,
    archive_reason: data.archive_reason,
    orders: data.orders as PairOrderRecoveryCheck['orders'],
  };
}

export function pairOrderRecoveryBlock(pair: Pair): string {
  if (pair.enabled) return '请先暂停配对组，再核对订单与持仓';
  const pending = pair.state?.pending;
  if (!pending || !Object.keys(pending).length)
    return '当前没有可手动核对的开仓批次';
  if (
    (pending.kind !== 'ordinary' && pending.kind !== 'cycle') ||
    pending.phase !== 'open'
  )
    return '仅支持符合条件的开仓待核对批次；减仓、杠杆或划转需继续原流程';
  if (typeof pending.id !== 'string' || !pending.id)
    return '当前批次缺少标识，请等待服务核对';
  if (pending.kind === 'cycle') {
    const progress = pair.state?.progress;
    const quantities = progress?.quantities;
    const before = pending.before;
    const owned = pair.state?.owned;
    const baseline = progress?.baseline;
    if (
      progress?.phase !== 'waiting_open' ||
      !record(quantities) ||
      Object.keys(quantities).length !== 2 ||
      !(['LONG', 'SHORT'] as const).every(
        (side) => normalizedQuantity(quantities[side]) === '0',
      )
    )
      return '循环状态或新增仓位不符合手动核对条件，请继续原恢复流程';
    if (
      !record(before) ||
      !record(owned) ||
      !record(baseline) ||
      !(['LONG', 'SHORT'] as const).every((side) => {
        const quantity = normalizedQuantity(before[side]);
        return (
          quantity !== null &&
          quantity === normalizedQuantity(owned[side]) &&
          quantity === normalizedQuantity(baseline[side])
        );
      })
    )
      return '循环底仓记录不一致或不完整，请继续原恢复流程';
    if (
      !Array.isArray(pending.legs) ||
      pending.legs.length !== 2 ||
      !pending.legs.every(
        (leg) =>
          record(leg) &&
          (leg.key === 'long' || leg.key === 'short') &&
          leg.receipt === null &&
          leg.dispatch === 'sending',
      ) ||
      new Set(pending.legs.map((leg) => leg.key)).size !== 2 ||
      !Array.isArray(pending.repairs) ||
      pending.repairs.length !== 0 ||
      pending.repair_attempts !== 0
    )
      return '仅支持两侧回执均未知且尚无补偿的循环开仓，请继续原恢复流程';
  }
  return '';
}

function normalizedQuantity(value: unknown): string | null {
  if (typeof value !== 'string' || !/^\d+(?:\.\d+)?$/.test(value)) return null;
  const [integer, fraction = ''] = value.split('.');
  const whole = integer.replace(/^0+(?=\d)/, '');
  const decimal = fraction.replace(/0+$/, '');
  return decimal ? `${whole}.${decimal}` : whole;
}

export function pairOrderRecoveryReviewBlock(
  review: PairOrderRecoveryReview,
  pair: Pair,
  now: number,
): string {
  const block = pairOrderRecoveryBlock(pair);
  if (block) return block;
  if (review.pair_id !== pair.id || review.batch_id !== pair.state?.pending?.id)
    return '配对组或待核对批次已变化，请重新读取';
  if (!Number.isFinite(now) || now <= 0 || now >= review.checked_at + 300)
    return '核对预览已过期，请重新读取';
  return '';
}

export function pairOrderRecoverySkipBlock(pair: Pair): string {
  const block = pairOrderRecoveryBlock(pair);
  if (block) return block;
  const pending = pair.state?.pending;
  if (pending?.kind !== 'ordinary')
    return '仅普通开仓批次可跳过持仓核对；循环开仓须核对订单与实际持仓';
  if (
    !pending ||
    !Array.isArray(pending.legs) ||
    pending.legs.length !== 2 ||
    !Array.isArray(pending.repairs)
  )
    return '订单记录不完整，无法跳过';
  const terminal = [
    'FILLED',
    'CANCELED',
    'REJECTED',
    'EXPIRED',
    'EXPIRED_IN_MATCH',
  ];
  if (
    ![...pending.legs, ...pending.repairs].every(
      (leg) =>
        record(leg) &&
        record(leg.receipt) &&
        typeof leg.receipt.status === 'string' &&
        terminal.includes(leg.receipt.status),
    )
  )
    return '仍有未知或活动订单，请继续按原订单编号核对';
  if (
    pair.state?.margin?.pending ||
    pair.state?.margin?.blocks_trading ||
    ['submitting', 'acknowledged', 'accepted', 'unknown'].includes(
      pair.state?.margin?.status ?? '',
    )
  )
    return '仍有未决划转，请先完成划转核对';
  if (
    Object.values(pair.state?.progress?.quantities ?? {}).some(
      (qty) => Number(qty) !== 0,
    )
  )
    return '仍有循环新增仓位，请继续原恢复流程';
  return '';
}

function record(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function quantities(value: unknown): value is Quantities {
  return (
    record(value) &&
    ['LONG', 'SHORT'].every(
      (side) =>
        typeof value[side] === 'string' && /^\d+(?:\.\d+)?$/.test(value[side]),
    )
  );
}

export type PairBaselineReview = {
  status: 'baseline_review';
  token: string;
  pair_id: string;
  checked_at: number;
  before: Quantities;
  actual: Quantities;
  leverage: number;
  orders: {
    batch_id: string;
    side: 'LONG' | 'SHORT';
    client_order_id: string;
    status: string;
    executed_qty: string | null;
  }[];
};

export function pairBaselineRecoveryBlock(pair: Pair): string {
  if (pair.enabled) return '请先暂停配对组';
  if (pair.state?.pending) return '请先完成当前待核对批次';
  if (!pair.state?.recovery_watch) return '当前没有人工归档订单';
  if (
    Object.values(pair.state?.progress?.quantities ?? {}).some(
      (qty) => Number(qty) !== 0,
    )
  )
    return '仍有循环新增仓位，请继续原恢复流程';
  if (
    pair.state?.margin?.pending ||
    ['submitting', 'acknowledged', 'accepted', 'unknown'].includes(
      pair.state?.margin?.status ?? '',
    )
  )
    return '仍有未决划转，请先完成划转核对';
  return '';
}

export function parsePairBaselineReview(
  data: unknown,
  pair: Pair,
): PairBaselineReview {
  if (
    !record(data) ||
    data.status !== 'baseline_review' ||
    data.pair_id !== pair.id ||
    typeof data.token !== 'string' ||
    !/^[a-f0-9]{32}$/.test(data.token) ||
    typeof data.checked_at !== 'number' ||
    !Number.isFinite(data.checked_at) ||
    data.checked_at <= 0 ||
    typeof data.leverage !== 'number' ||
    !Number.isInteger(data.leverage) ||
    data.leverage <= 0 ||
    !quantities(data.before) ||
    !quantities(data.actual) ||
    !Array.isArray(data.orders) ||
    data.orders.length < 2 ||
    data.orders.length > 16 ||
    !data.orders.every(
      (order) =>
        record(order) &&
        typeof order.batch_id === 'string' &&
        Boolean(order.batch_id) &&
        (order.side === 'LONG' || order.side === 'SHORT') &&
        typeof order.client_order_id === 'string' &&
        Boolean(order.client_order_id) &&
        ((order.status === 'UNKNOWN' && order.executed_qty === null) ||
          ([
            'FILLED',
            'CANCELED',
            'REJECTED',
            'EXPIRED',
            'EXPIRED_IN_MATCH',
          ].includes(String(order.status)) &&
            typeof order.executed_qty === 'string' &&
            /^0(?:\.0+)?$/.test(order.executed_qty))),
    ) ||
    new Set(data.orders.map((order) => order.side)).size !== 2
  )
    throw new Error('底仓预览响应不完整或包含未解决成交，请重新核对');
  return data as PairBaselineReview;
}

export function parsePairOrderRecovery(
  data: unknown,
  pair: Pair,
): PairOrderRecoveryReview | ReceiptsFound {
  if (!record(data) || typeof data.message !== 'string')
    throw new Error('核对响应不完整，请重新读取');
  if (data.status === 'receipts_found')
    return { status: 'receipts_found', message: data.message };
  if (data.status !== 'review') throw new Error('核对响应状态无效，请重新读取');
  if (data.pair_id !== pair.id || data.batch_id !== pair.state?.pending?.id)
    throw new Error('核对响应与当前配对组或批次不符，请重新读取');
  if (
    typeof data.token !== 'string' ||
    !data.token ||
    typeof data.created_at !== 'number' ||
    !Number.isFinite(data.created_at) ||
    data.created_at <= 0 ||
    typeof data.checked_at !== 'number' ||
    !Number.isFinite(data.checked_at) ||
    data.checked_at <= 0 ||
    typeof data.leverage !== 'number' ||
    !Number.isFinite(data.leverage) ||
    data.leverage <= 0 ||
    !quantities(data.before) ||
    !quantities(data.actual) ||
    !Array.isArray(data.orders) ||
    data.orders.length !== 2 ||
    !data.orders.every(
      (order) =>
        record(order) &&
        (order.side === 'LONG' || order.side === 'SHORT') &&
        typeof order.client_order_id === 'string' &&
        Boolean(order.client_order_id) &&
        order.result === 'not_found',
    ) ||
    new Set(data.orders.map((order) => order.side)).size !== 2
  )
    throw new Error('核对响应不完整，请重新读取');
  return data as PairOrderRecoveryReview;
}
