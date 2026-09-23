'use client';
import { useState } from 'react';
import type { DeskAction, Listings, State } from '@/lib/desk-types';
import { fmt } from '@/lib/desk-format';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

const date = (value?: number | null) =>
  value
    ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false })
    : '—';
const statuses: Record<string, string> = {
  TRADING: '交易中',
  PENDING_TRADING: '待上线',
  PRE_SETTLE: '预结算',
  SETTLING: '结算中',
  CLOSE: '已下架',
  MISSING: '本次列表未返回',
};

export function ListingsPanel({
  listings,
  notification,
  now,
  connectionError,
  action,
  busy,
}: {
  listings?: Listings;
  notification?: State['notification'];
  now: number;
  connectionError: string;
  action: DeskAction;
  busy: boolean;
}) {
  const [pendingWatch, setPendingWatch] = useState<{
    symbol: string;
    enabled: boolean;
  } | null>(null);
  const toggleWatch = async (symbol: string, enabled: boolean) => {
    if (busy || pendingWatch) return;
    setPendingWatch({ symbol, enabled });
    try {
      await action(
        `/api/listings/${encodeURIComponent(symbol)}/capacity-alert`,
        { enabled },
        'PATCH',
      );
    } finally {
      setPendingWatch(null);
    }
  };
  const rows = Object.values(listings?.rows ?? {}).sort(
    (a, b) =>
      Number(Boolean(b.is_new)) - Number(Boolean(a.is_new)) ||
      (b.onboard_at ?? 0) - (a.onboard_at ?? 0) ||
      a.symbol.localeCompare(b.symbol),
  );
  const stale = (stamp?: number | null) =>
    !stamp ||
    now - stamp < -1 ||
    now - stamp >= (listings?.stale_seconds ?? 180);
  const catalogStale =
    Boolean(connectionError || listings?.error) || stale(listings?.checked_at);
  const watched = new Set(listings?.watched_symbols ?? []);
  return (
    <section className="panel listings-panel">
      <div className="section-head">
        <div>
          <h2>USD1 交易对上新</h2>
          <p>
            每 {listings?.poll_seconds ?? 60} 秒检查永续交易对 ·
            关闭网页后继续监控
          </p>
        </div>
        <span className="small-note">{rows.length} 个交易对</span>
      </div>
      <div className="listings-summary">
        <p>
          首次运行建立现有交易对基线，之后发现新上线即推送飞书。公开可用额度 =
          该最大杠杆的公开剩余额度与档位上限的较小值，单位 USD1。
        </p>
        <p>
          勾选即保存。最大杠杆可用额度 &gt; 0
          时提醒一次，持续有额度不重复；归零后恢复、最大杠杆变化或重新勾选时再次提醒。无需启动账户。
        </p>
        <p className={notification?.error ? 'amber' : 'muted'}>
          {notification?.error ||
            (notification?.configured
              ? `飞书已配置${notification.pending ? ` · ${notification.pending} 条通知待发送` : ''}`
              : '飞书未配置，请在服务器配置 FEISHU_WEBHOOK_URL')}{' '}
          · 公开额度随市场变化，不代表账户实际可开额度。
        </p>
        <p className={catalogStale ? 'amber' : 'muted'}>
          {listings?.enabled === false
            ? '模拟环境不运行上新监控或发送通知'
            : listings?.error ||
              (catalogStale ? '等待有效列表 / 列表已过期' : '监控运行中')}{' '}
          · 列表检查 {date(listings?.checked_at)}
        </p>
      </div>
      {rows.length ? (
        <section
          className="table-scroll"
          aria-label="USD1 上新与最大杠杆额度，可横向滚动"
        >
          <Table className="listings-table">
            <TableHeader>
              <TableRow>
                <TableHead>额度 &gt; 0 提醒</TableHead>
                <TableHead>交易对 / 状态</TableHead>
                <TableHead className="number">最大杠杆</TableHead>
                <TableHead className="number">公开可用额度 · USD1</TableHead>
                <TableHead className="number">公开剩余 / 档位上限</TableHead>
                <TableHead>上线 / 发现时间</TableHead>
                <TableHead>额度采样 / 数据状态</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => {
                const unavailable =
                  catalogStale ||
                  row.status !== 'TRADING' ||
                  Boolean(row.error) ||
                  stale(row.checked_at);
                return (
                  <TableRow key={row.symbol}>
                    <TableCell>
                      <label className="listing-watch-toggle">
                        <input
                          type="checkbox"
                          aria-label={`${row.symbol} 最大杠杆额度大于零时提醒`}
                          checked={
                            pendingWatch?.symbol === row.symbol
                              ? pendingWatch.enabled
                              : watched.has(row.symbol)
                          }
                          disabled={
                            busy ||
                            Boolean(connectionError) ||
                            !listings?.enabled
                          }
                          onChange={(event) =>
                            void toggleWatch(row.symbol, event.target.checked)
                          }
                        />
                        {pendingWatch?.symbol === row.symbol
                          ? '保存中…'
                          : watched.has(row.symbol)
                            ? '已勾选'
                            : '勾选'}
                      </label>
                    </TableCell>
                    <TableCell>
                      <strong>{row.symbol}</strong>
                      <small>
                        {row.is_new ? '新发现 · ' : ''}
                        {statuses[row.status] ?? row.status}
                      </small>
                    </TableCell>
                    <TableCell className="number">
                      {row.max_leverage == null ? '—' : `${row.max_leverage}x`}
                      {row.max_leverage != null &&
                      (catalogStale || stale(row.brackets_checked_at)) ? (
                        <small className="amber">上次档位</small>
                      ) : null}
                    </TableCell>
                    <TableCell
                      className={`number ${unavailable ? 'muted' : ''}`}
                    >
                      {fmt(row.capacity)}
                      {row.capacity != null && unavailable ? (
                        <small className="amber">上次采样</small>
                      ) : null}
                    </TableCell>
                    <TableCell className="number">
                      {fmt(row.remaining)}
                      <small>上限 {fmt(row.bracket_cap)}</small>
                    </TableCell>
                    <TableCell>
                      {date(row.onboard_at)}
                      <small>发现 {date(row.detected_at)}</small>
                    </TableCell>
                    <TableCell>
                      {date(row.checked_at)}
                      <small className={unavailable ? 'amber' : 'muted'}>
                        {row.error ||
                          (unavailable ? '等待更新 / 数据已过期' : '有效采样')}
                      </small>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </section>
      ) : (
        <p className="listings-summary muted">
          {listings?.enabled === false
            ? '真实服务启动后自动检查 USD1 交易对。'
            : '正在等待首次公开交易对列表。'}
        </p>
      )}
    </section>
  );
}
