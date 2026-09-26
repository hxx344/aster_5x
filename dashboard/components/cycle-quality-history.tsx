'use client';
import { useState } from 'react';
import { CycleExecutionQualityPanel } from '@/components/cycle-execution-quality';
import {
  cycleExecutionQualityView,
  cycleQualityHistoryGroup,
  type CycleExecutionQuality,
  type CycleQualityHistory,
} from '@/lib/cycle-quality';

export function CycleQualityHistoryPanel({
  history,
  quality,
  accountName,
  stale,
}: {
  history?: CycleQualityHistory | null;
  quality?: CycleExecutionQuality | null;
  accountName: string;
  stale: boolean;
}) {
  const [selection, setSelection] = useState('');
  const group = cycleQualityHistoryGroup(history, selection, quality);
  return (
    <div className="feature-stack">
      <section className="panel cycle-quality-history">
        <div className="cycle-quality-heading">
          <h3>最近 100 次请求 · 最佳与最差</h3>
          <span>{accountName}</span>
        </div>
        <p className="cycle-quality-note">
          按“请求开始 →
          收到响应”耗时排名，最短为最佳、最长为最差。每个品种的开仓、平仓分别统计，排名不代表成交价差优劣。
        </p>
        {stale ? (
          <p className="cycle-quality-notice amber">
            状态同步中断，以下为最近记录。
          </p>
        ) : null}
        {group ? (
          <>
            <label className="cycle-quality-group">
              统计范围
              <select
                value={`${group.symbol}:${group.phase}`}
                onChange={(event) => setSelection(event.target.value)}
              >
                {history?.groups.map((item) => (
                  <option
                    key={`${item.symbol}:${item.phase}`}
                    value={`${item.symbol}:${item.phase}`}
                  >
                    {item.symbol} · {item.phase === 'open' ? '开仓' : '平仓'}
                  </option>
                ))}
              </select>
            </label>
            <p className="cycle-quality-note">
              最近 {group.count} / 100 次请求，{group.comparable_count}{' '}
              次可比较，{group.count - group.comparable_count}{' '}
              次无有效响应耗时。
              每个原始双边批次计一次，本地未发送、修复和回执查询不计入。记录已保存，重启后保留。
            </p>
            {group.best && group.best.intent_id === group.worst?.intent_id ? (
              <p className="cycle-quality-note">
                最佳和最差为同一条记录；耗时并列时展示较新的请求。
              </p>
            ) : null}
            {(['best', 'worst'] as const).map((kind) => {
              const record = group[kind];
              const view = cycleExecutionQualityView(record);
              const title =
                kind === 'best'
                  ? '最佳请求（耗时最短）'
                  : '最差请求（耗时最长）';
              return view ? (
                <details
                  className="cycle-quality-extreme"
                  key={`${group.symbol}:${group.phase}:${kind}`}
                >
                  <summary>
                    <strong>{title}</strong>
                    <span>{view.timing[2].value} ms</span>
                    <span>{view.actual.status}</span>
                    <time>{view.requestStartedAt}</time>
                  </summary>
                  <div className="cycle-quality-detail-body">
                    <CycleExecutionQualityPanel
                      quality={record}
                      accountName={accountName}
                      stale={false}
                      title={`${title} · 完整成交质量`}
                    />
                  </div>
                </details>
              ) : (
                <p className="cycle-quality-note" key={kind}>
                  {title}：暂无可比较记录。
                </p>
              );
            })}
          </>
        ) : (
          <p className="cycle-quality-note">
            尚无已发送请求记录。记录有效请求后，将自动保存并展示最佳与最差明细。
          </p>
        )}
      </section>
      <section className="panel">
        <CycleExecutionQualityPanel
          quality={quality}
          accountName={accountName}
          stale={stale}
        />
      </section>
    </div>
  );
}
