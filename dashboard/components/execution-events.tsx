import { Activity } from 'lucide-react';
import { CycleDiagnostic } from '@/components/cycle-diagnostic';
import { cycleUtcTime } from '@/lib/cycle-daily';
import { executionEventRows, type ExecutionEvent } from '@/lib/cycle-events';

export function ExecutionEvents({ events }: { events: ExecutionEvent[] }) {
  const rows = executionEventRows(events).slice(0, 50);
  return (
    <div className="event-list">
      {rows.length ? (
        rows.map((row) => {
          const event = row.event;
          const isCheck = row.type === 'check';
          return (
            <div
              className={`event${isCheck ? ' event-check' : ''}`}
              key={row.key}
            >
              <time title={cycleUtcTime(event.created_at)}>
                {new Date(event.created_at * 1000).toLocaleTimeString('zh-CN', {
                  hour12: false,
                })}
              </time>
              <i
                aria-hidden="true"
                className={`event-dot ${isCheck || event.kind === 'wait' ? 'amber-bg' : event.kind === 'error' ? 'danger-bg' : 'mint-bg'}`}
              />
              {isCheck ? (
                <div className="event-check-content">
                  <p className="event-check-summary">{row.summary}</p>
                  <p className="event-check-meta">
                    <span>
                      {row.symbol || '品种未记录'} ·{' '}
                      {row.phase === 'open'
                        ? '开仓检查'
                        : row.phase === 'close'
                          ? '平仓检查'
                          : '阶段未记录'}
                    </span>
                    <span>
                      合并 {row.count} 次{row.legacy ? '（已加载旧记录）' : ''}
                    </span>
                  </p>
                  <p className="event-check-range">
                    <span>
                      首次 <time>{cycleUtcTime(row.firstAt)}</time>
                    </span>
                    <span>
                      最近 <time>{cycleUtcTime(row.lastAt)}</time>
                    </span>
                  </p>
                  <details className="event-check-details">
                    <summary>最新详情</summary>
                    <div className="event-check-detail-body">
                      <p>{event.message}</p>
                      <CycleDiagnostic diagnostic={row.diagnostic} />
                    </div>
                  </details>
                </div>
              ) : (
                <p className="event-message">{event.message}</p>
              )}
            </div>
          );
        })
      ) : (
        <div className="empty-state">
          <Activity size={27} />
          <h3>暂无执行记录</h3>
          <p>开仓、杠杆调整与异常处理都会显示在这里。</p>
        </div>
      )}
    </div>
  );
}
