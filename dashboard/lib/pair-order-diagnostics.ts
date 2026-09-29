import type { Pair } from './pairs.ts';

export type PairOrderDiagnostic = {
  id: string;
  sideLabel: string;
  stageLabel: string;
  clientOrderId: string;
  statusLabel: string;
  executedQty: string;
  error: string;
  initialError?: string;
  terminal: boolean;
};

const statusLabels: ReadonlyMap<string, string> = new Map([
  ['NEW', '已受理 · 待成交'],
  ['PARTIALLY_FILLED', '部分成交'],
  ['FILLED', '全部成交'],
  ['CANCELED', '已撤销'],
  ['PENDING_CANCEL', '撤单待确认'],
  ['EXPIRED', '已失效'],
  ['EXPIRED_IN_MATCH', '撮合时失效'],
  ['REJECTED', '已拒绝'],
]);
const terminalStatuses = new Set([
  'FILLED',
  'CANCELED',
  'EXPIRED',
  'EXPIRED_IN_MATCH',
  'REJECTED',
]);

function record(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function text(value: unknown, fallback = ''): string {
  return typeof value === 'string' && value.trim() ? value : fallback;
}

function pendingOrders(pair: Pair): Record<string, unknown> | undefined {
  const pending = record(record(record(pair)?.state)?.pending);
  return pending?.kind === 'ordinary' || pending?.kind === 'cycle'
    ? pending
    : undefined;
}

export function hasPendingPairOrders(pair: Pair): boolean {
  return pendingOrders(pair) !== undefined;
}

export function pendingOrderDiagnostics(pair: Pair): PairOrderDiagnostic[] {
  const pending = pendingOrders(pair);
  if (!pending) return [];

  const diagnostics: PairOrderDiagnostic[] = [];
  for (const kind of ['legs', 'repairs'] as const) {
    const legs = pending[kind];
    if (!Array.isArray(legs)) continue;
    legs.forEach((value, index) => {
      const leg = record(value);
      if (!leg) return;
      const order = record(leg.order);
      const receipt = record(leg.receipt);
      const clientOrderId = text(order?.newClientOrderId, '—');
      const status = text(receipt?.status);
      const executedQty = receipt?.executedQty;
      const error = text(leg.error, text(receipt?.reject_reason)).slice(
        0,
        1000,
      );
      const initialError = text(leg.submit_error).slice(0, 1000);
      diagnostics.push({
        id: `${kind}:${index}:${clientOrderId}`,
        sideLabel:
          leg.key === 'long'
            ? 'A · 只多'
            : leg.key === 'short'
              ? 'B · 只空'
              : '未知侧',
        stageLabel:
          kind === 'repairs'
            ? '恢复减仓'
            : pending.phase === 'close'
              ? '本批减仓'
              : '本批开仓',
        clientOrderId,
        statusLabel:
          status === 'REJECTED' && receipt?.local_not_sent === true
            ? '本地未发送'
            : status
              ? (statusLabels.get(status) ?? `未知状态 · ${status}`)
              : leg.dispatch === 'prepared'
                ? '尚未进入发送阶段'
                : '尚无可核验回执',
        executedQty:
          typeof executedQty === 'string' &&
          executedQty.trim() === executedQty &&
          /^\d+(?:\.\d+)?$/.test(executedQty)
            ? executedQty
            : '—',
        error,
        ...(initialError && initialError !== error ? { initialError } : {}),
        // This is a display hint only; execution recovery remains server-owned.
        terminal: terminalStatuses.has(status),
      });
    });
  }
  return diagnostics;
}
