import type { CycleWindowCost } from '@/lib/cycle';
import { cycleCostSummary } from '@/lib/cycle-daily';

export function CycleCostSummary({
  label,
  cost,
  stale,
}: {
  label: string;
  cost?: CycleWindowCost;
  stale: boolean;
}) {
  const view = cycleCostSummary(cost, stale);
  return (
    <section className="cycle-cost-summary" aria-label={label}>
      <h4>{label} · USD1</h4>
      <dl className="cycle-cost-grid">
        <div>
          <dt>手续费 · 固定 0.0125%</dt>
          <dd>{view.fee}</dd>
        </div>
        <div>
          <dt>已配对差价</dt>
          <dd>{view.spread}</dd>
        </div>
        <div className="cycle-cost-total">
          <dt>已计总成本</dt>
          <dd>{view.total}</dd>
        </div>
      </dl>
      {view.staleNotice ? <p className="amber">{view.staleNotice}</p> : null}
      {view.notice ? <p className="amber">{view.notice}</p> : null}
      {view.hasUnmatched ? (
        <p>
          待配对成交 {view.unmatchedCount} 笔 · {view.unmatched} USD1
        </p>
      ) : null}
    </section>
  );
}
