'use client';

import { ChevronDown, Radio } from 'lucide-react';
import type { CapacityRelayStatus } from '@/lib/desk-types';
import {
  capacityRelayView,
  relayDuration,
  relayTime,
} from '@/lib/capacity-relay';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

const count = (value?: number) =>
  typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? String(value)
    : '—';

export function CapacityRelayPanel({
  relay,
  now,
  updatedAt,
  connectionError,
  demo,
}: {
  relay?: CapacityRelayStatus | null;
  now: number;
  updatedAt?: number;
  connectionError?: string;
  demo: boolean;
}) {
  const view = capacityRelayView(relay, {
    now,
    updatedAt,
    connectionError,
    demo,
  });
  const ws = relay?.ws;
  const http = relay?.http;
  const observationAge =
    typeof relay?.observed_at === 'number' &&
    Number.isFinite(relay.observed_at) &&
    relay.observed_at > 0 &&
    Number.isFinite(now) &&
    now >= relay.observed_at
      ? now - relay.observed_at
      : null;
  const retry =
    ws?.retry_in_seconds != null && Number.isFinite(ws.retry_in_seconds)
      ? Math.max(0, ws.retry_in_seconds - (observationAge ?? 0))
      : null;
  const detailed = !demo && Boolean(ws || http || relay?.samples);

  return (
    <details className="panel relay-status" aria-label="副服务器额度连接">
      <summary>
        <span className="relay-status-title">
          <Radio size={17} aria-hidden="true" />
          <strong>副服务器额度连接</strong>
        </span>
        <span className="relay-status-overview">
          <span className={`relay-status-label relay-tone-${view.tone}`}>
            <i aria-hidden="true" />
            {view.connection}
          </span>
          <span className="relay-status-data">{view.data}</span>
        </span>
        <span className="relay-status-toggle">
          <span className="relay-show-details">详情</span>
          <span className="relay-hide-details">收起</span>
          <ChevronDown size={16} aria-hidden="true" />
        </span>
      </summary>
      <div className="relay-status-content">
        <p
          className={
            view.stale ? 'relay-status-note amber' : 'relay-status-note'
          }
        >
          {view.note}
        </p>
        {detailed ? (
          <>
            <div className="relay-transport-grid">
              <section aria-label="WS 连接详情">
                <h3>WS 推送</h3>
                <p className="relay-transport-caption">主服务器 → 副服务器</p>
                <dl className="relay-facts">
                  <div>
                    <dt>最近连接</dt>
                    <dd>{relayTime(ws?.connected_at)}</dd>
                  </div>
                  <div>
                    <dt>最近断开</dt>
                    <dd>{relayTime(ws?.disconnected_at)}</dd>
                  </div>
                  <div>
                    <dt>最近收到消息</dt>
                    <dd>{relayTime(ws?.last_message_at)}</dd>
                  </div>
                  <div>
                    <dt>最近接纳新样本</dt>
                    <dd>{relayTime(ws?.last_sample_at)}</dd>
                  </div>
                  <div>
                    <dt>连接尝试次数</dt>
                    <dd>{count(ws?.connection_attempts)}</dd>
                  </div>
                  <div>
                    <dt>{view.stale ? '上次重连计划' : '下次重连'}</dt>
                    <dd>
                      {view.stale
                        ? relayDuration(ws?.retry_in_seconds)
                        : retry == null
                          ? '—'
                          : retry > 0
                            ? `${relayDuration(retry)}后`
                            : '等待重试'}
                    </dd>
                  </div>
                </dl>
                {view.wsError ? (
                  <p className="relay-transport-error">{view.wsError}</p>
                ) : null}
              </section>
              <section aria-label="HTTP 缓存补取详情">
                <h3>HTTP 缓存补取</h3>
                <p className="relay-transport-caption">
                  只读取副服缓存，不触发 Aster 查询
                </p>
                <dl className="relay-facts">
                  <div>
                    <dt>正在请求</dt>
                    <dd>{count(http?.inflight)}</dd>
                  </div>
                  <div>
                    <dt>累计请求</dt>
                    <dd>{count(http?.requests)}</dd>
                  </div>
                  <div>
                    <dt>累计失败</dt>
                    <dd>{count(http?.failures)}</dd>
                  </div>
                  <div>
                    <dt>最近请求</dt>
                    <dd>{relayTime(http?.last_attempt_at)}</dd>
                  </div>
                  <div>
                    <dt>最近有效响应</dt>
                    <dd>{relayTime(http?.last_success_at)}</dd>
                  </div>
                </dl>
                {view.httpError ? (
                  <p className="relay-transport-error">{view.httpError}</p>
                ) : null}
              </section>
            </div>
            <div className="relay-samples-heading">
              <h3>已收到的额度样本</h3>
              <span>
                {view.stale ? '最近记录' : '状态采集'} ·{' '}
                {relayTime(relay?.observed_at)} · 页面状态年龄{' '}
                {relayDuration(observationAge)}
              </span>
            </div>
            {view.samples.length ? (
              <Table
                className="relay-samples-table"
                aria-label="副服务器额度样本详情"
              >
                <TableHeader>
                  <TableRow>
                    <TableHead>交易对</TableHead>
                    <TableHead>数据</TableHead>
                    <TableHead>最近来源</TableHead>
                    <TableHead>采集时样本年龄</TableHead>
                    <TableHead>采集时新鲜度</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {view.samples.map((sample) => (
                    <TableRow key={sample.key}>
                      <TableCell>{sample.symbol}</TableCell>
                      <TableCell>{sample.kindLabel}</TableCell>
                      <TableCell>{sample.sourceLabel}</TableCell>
                      <TableCell className="relay-sample-age">
                        {sample.ageLabel}
                      </TableCell>
                      <TableCell className={`relay-tone-${sample.tone}`}>
                        {sample.freshness}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            ) : (
              <p className="relay-empty-samples">尚未收到可展示的额度样本。</p>
            )}
            <p className="relay-status-footnote">
              仅展示已收到的样本，不代表全部目标交易对。额度缓存最长 8
              秒，循环要求 1 秒内；杠杆档位最长 5
              分钟。连接正常或缓存新鲜不代表已通过开仓检查。
            </p>
            <p className="relay-status-footnote">
              页面沿用约 3
              秒一次的状态刷新；表格显示主服务器采集状态时的样本年龄，不叠加页面等待时间。两次页面刷新之间主服务器仍可接收新样本，实际开仓按当时最新样本校验。请求次数在主服务重启后重新计数。
            </p>
          </>
        ) : null}
      </div>
    </details>
  );
}
