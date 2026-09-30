import { cycleUtcTime } from '@/lib/cycle-daily';
import {
  pairCostFresh,
  pairCostPeriodView,
  type PairCostPeriod,
  type PairCycleCosts,
} from '@/lib/pair-cost';

export function PairCycleCostsPanel({
  report,
  now,
  offline,
}: {
  report?: PairCycleCosts | null;
  now: number;
  offline: boolean;
}) {
  const stale = Boolean(report) && !pairCostFresh(report, now, offline);
  return (
    <section className="panel pair-cycle-costs" aria-label="两侧循环花费">
      <div className="section-head">
        <div>
          <h2>两侧循环花费</h2>
          <p className="muted">USD1 · UTC 自然日 / 周一开始的自然周</p>
        </div>
        <span
          className={`small-note pair-cost-updated ${stale ? 'amber' : ''}`}
        >
          {report
            ? `统计截至 ${cycleUtcTime(report.as_of)}${stale ? ' · 待更新' : ''}`
            : '等待统计'}
        </span>
      </div>
      {stale ? (
        <p className="amber">花费数据已过期或连接中断，以下保留上次统计。</p>
      ) : null}
      <div className="pair-cost-periods">
        <CostPeriod title="今日" period={report?.daily} />
        <CostPeriod title="本周" period={report?.weekly} />
      </div>
      <details className="pair-cost-method">
        <summary>计算口径</summary>
        <p>
          估算手续费按两侧循环开仓、减回及修复的实际成交金额 × 0.0125% 计算。
          总花费 = 估算手续费 +
          成交价差；成交价差已包含滑点，执行滑点不再重复相加。
        </p>
        <p>
          执行滑点比较原始订单成交价与下单前深度均价，正值表示成交变差，负值表示成交改善。
          修复成交或旧记录缺少参考价时仅显示已知部分；未配平、未知时间及跨期成交会标记不完整。
        </p>
        <p>
          UTC 00:00 对应北京时间
          08:00。仅统计当前配对组的循环成交，不含普通底仓、资金费、返佣或外部交易。
        </p>
      </details>
    </section>
  );
}

function CostPeriod({
  title,
  period,
}: {
  title: string;
  period?: PairCostPeriod;
}) {
  const view = pairCostPeriodView(period);
  return (
    <section className="pair-cost-period" aria-label={`${title}循环花费`}>
      <div className="pair-cost-period-heading">
        <h3>{title}</h3>
        <span className="small-note">{view.range} · UTC</span>
      </div>
      <dl className="cycle-cost-grid">
        <div className="cycle-cost-total">
          <dt>
            {view.complete
              ? '总花费 · 含估算手续费'
              : '已统计花费小计 · 未完整'}
          </dt>
          <dd>{view.total}</dd>
        </div>
        <div>
          <dt>估算手续费 · 固定 0.0125%</dt>
          <dd>{view.fee}</dd>
        </div>
        <div>
          <dt>成交价差 · 含滑点</dt>
          <dd>{view.spread}</dd>
        </div>
        <div>
          <dt>执行滑点{view.slippageComplete ? '' : ' · 未完整'}</dt>
          <dd>{view.slippage}</dd>
        </div>
      </dl>
      <p className="muted">已计 {view.count} 笔成交订单</p>
      {view.notices.map((notice) => (
        <p className="amber" key={notice}>
          {notice}
        </p>
      ))}
      {period && !view.slippageComplete ? (
        <p className="amber">滑点参考价或成交数据不完整，不能视为零滑点。</p>
      ) : null}
    </section>
  );
}
