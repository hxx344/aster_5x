'use client';
import { clock } from '@/lib/desk-format';
import {
  hasPendingPairOrders,
  pendingOrderDiagnostics,
} from '@/lib/pair-order-diagnostics';
import type { Pair } from '@/lib/pairs';

export function PairOrderDiagnostics({ pair }: { pair: Pair }) {
  if (!hasPendingPairOrders(pair)) return null;
  const orders = pendingOrderDiagnostics(pair);
  const createdAt = pair.state?.pending?.created_at;
  return (
    <section className="pair-notices" aria-label="本批订单核对明细">
      <div className="pair-notices-heading">
        <span>本批订单核对明细</span>
        {typeof createdAt === 'number' &&
        Number.isFinite(createdAt) &&
        createdAt > 0 ? (
          <span className="muted small-note">批次创建 {clock(createdAt)}</span>
        ) : null}
      </div>
      <div className="pair-order-diagnostics-list">
        {orders.map((order) => (
          <div key={order.id} className="pair-order-diagnostic">
            <p>
              <strong>{order.sideLabel}</strong> · {order.stageLabel}
            </p>
            <p className={order.terminal ? '' : 'amber'}>
              回执记录：{order.statusLabel} · 累计成交 {order.executedQty} XAU
            </p>
            <p className="muted small-note">
              原客户端订单编号：{order.clientOrderId}
            </p>
            {order.initialError ? (
              <p className={order.terminal ? 'muted' : 'amber'}>
                最初下单反馈：{order.initialError}
              </p>
            ) : null}
            {order.error ? (
              <p className={order.terminal ? 'muted' : 'amber'}>
                {order.initialError ? '最近查询反馈' : '最近反馈'}：
                {order.error}
              </p>
            ) : !order.terminal ? (
              <p className="muted">等待原订单查询取得有效终态回执。</p>
            ) : null}
          </div>
        ))}
      </div>
      {!orders.length ? (
        <p className="amber">
          现有记录未包含可展示的逐腿信息，尚不能判断核对阻塞原因。
        </p>
      ) : null}
      <p className="muted pair-notice-scope">
        持仓卡片时间是账户快照时间，不是订单查询时间。订单未核实期间可能保留旧快照；两侧数量相等不能替代本批成交核对。
      </p>
    </section>
  );
}
