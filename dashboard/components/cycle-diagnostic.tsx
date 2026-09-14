import type { CycleDiagnostic as Diagnostic } from '@/lib/cycle';
import { cycleDiagnosticView } from '@/lib/cycle-diagnostic';
import { cycleUtcTime } from '@/lib/cycle-daily';

export function CycleDiagnostic({
  diagnostic,
}: {
  diagnostic?: Diagnostic | null;
}) {
  const view = cycleDiagnosticView(diagnostic);
  if (!view) return null;
  return (
    <section className="cycle-diagnostic" aria-label="循环失败详情">
      <div className="cycle-diagnostic-heading">
        <h3>上次检查结果</h3>
        <span>
          {view.symbol} · {view.phase}
        </span>
      </div>
      <p className="cycle-diagnostic-title">{view.title}</p>
      <p className="cycle-diagnostic-time">
        上次失败检查：<time>{cycleUtcTime(view.checkedAt)}</time>
      </p>
      <p className="cycle-diagnostic-note">未列出的条件不代表已通过。</p>
      {view.checks.length ? (
        <ul className="cycle-diagnostic-checks">
          {view.checks.map((check) => (
            <li key={check.key}>
              <div className="cycle-diagnostic-check-head">
                <strong>{check.label}</strong>
                <span className={check.tone}>{check.status}</span>
              </div>
              <dl className="cycle-diagnostic-values">
                <div>
                  <dt>实际值{check.unit ? ` · ${check.unit}` : ''}</dt>
                  <dd>{check.actual}</dd>
                </div>
                <div>
                  <dt>要求值{check.unit ? ` · ${check.unit}` : ''}</dt>
                  <dd>{check.required}</dd>
                </div>
              </dl>
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">此次诊断未提供具体检查项。</p>
      )}
      {view.context.length ? (
        <dl className="cycle-diagnostic-context cycle-diagnostic-values">
          {view.context.map((item) => (
            <div key={item.key}>
              <dt>
                {item.label}
                {item.unit ? ` · ${item.unit}` : ''}
              </dt>
              <dd>{item.value}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      {view.note ? <p className="cycle-diagnostic-note">{view.note}</p> : null}
    </section>
  );
}
