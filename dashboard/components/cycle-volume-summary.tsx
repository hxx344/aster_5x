import type { Account } from '@/lib/desk-types';
import {
  cycleAmount,
  cycleDailySummary,
  cycleReportSummary,
  cycleRollingSummary,
} from '@/lib/cycle-daily';
import { CycleCostSummary } from '@/components/cycle-cost-summary';
import { cycleHasPosition } from '@/lib/cycle';
export function CycleVolumeSummary({
  account,
  now,
  stale,
}: {
  account: Account;
  now: number;
  stale: boolean;
}) {
  const state = account.cycle_state;
  const report = cycleReportSummary(state?.report_status, now);
  const reportStale = stale || report.stale;
  const daily = cycleDailySummary(state?.daily_volume, now, reportStale);
  const rolling = cycleRollingSummary(state?.rolling_volume, now, reportStale);
  const waiting =
    ['daily_limit', 'rolling_limit'].includes(state?.phase ?? '') ||
    state?.daily_volume?.reached ||
    state?.rolling_volume?.reached;
  return (
    <section className="panel volume-panel" aria-label="循环成交额度">
      <div className="section-head">
        <h2>成交额度 · USD1</h2>
        <span className="small-note">{account.cycle?.symbol ?? 'XAUUSD1'}</span>
      </div>
      <div className="volume-windows">
        <section>
          <h3>
            UTC 当日 <small>{daily.date}</small>
          </h3>
          <strong>{daily.volume}</strong>
          <p>
            剩余 <b>{daily.remaining}</b> · 上限 {daily.limit}
          </p>
          {daily.notice ? <p className="amber">{daily.notice}</p> : null}
        </section>
        <section>
          <h3>滚动 24 小时</h3>
          <strong>{rolling.volume}</strong>
          <p>
            剩余 <b>{rolling.remaining}</b> · 上限 {daily.limit}
          </p>
          {rolling.notice ? <p className="amber">{rolling.notice}</p> : null}
        </section>
      </div>
      {report.notice ? (
        <p className={`volume-stamp ${reportStale ? 'amber' : 'muted'}`}>
          {report.notice}
        </p>
      ) : null}
      {account.cycle?.enabled && waiting ? (
        <p className="quota-notice amber">
          {!account.enabled
            ? '账户已暂停，额度释放后仍需手动启动。'
            : `${cycleHasPosition(state) ? '继续本轮条件平仓，暂停新增。' : '额度不足，暂停新增。'}两项额度和交易条件均满足后自动恢复；暂停账户可取消自动恢复。`}
        </p>
      ) : null}
      <details className="disclosure inset">
        <summary>成本、释放时间与各品种统计</summary>
        <div className="volume-windows">
          <section>
            <CycleCostSummary
              label="UTC 当日成本"
              cost={state?.daily_volume?.cost}
              stale={reportStale || daily.rolloverPending}
            />
            <p>当日成交 {daily.trades} 笔</p>
            <p>重置：{daily.resetAt}</p>
          </section>
          <section>
            <CycleCostSummary
              label="近 24 小时成本"
              cost={state?.rolling_volume?.cost}
              stale={rolling.stale}
            />
            <p>成交 {rolling.trades} 笔</p>
            <p>下一笔释放：{rolling.releaseAt}</p>
            <p>
              窗口：{rolling.windowStart} 至 {rolling.windowEnd}
            </p>
            {rolling.hasEstimates ? (
              <p className="amber">
                {rolling.estimatedVolume} USD1 使用旧模拟成交的估算时间。
              </p>
            ) : null}
          </section>
        </div>
        {state?.volume_by_symbol ? (
          <dl className="migration-details">
            {Object.entries(state.volume_by_symbol).map(([symbol, volumes]) => (
              <div key={symbol}>
                <dt>
                  {symbol}
                  {symbol === account.cycle?.symbol ? ' · 当前' : ''}
                </dt>
                <dd>
                  当日 {cycleAmount(volumes.daily_volume.volume)}
                  <small>
                    24h {cycleAmount(volumes.rolling_volume.volume)}
                  </small>
                </dd>
              </div>
            ))}
          </dl>
        ) : null}
        <p className="muted rules-copy">
          各品种独立累计，日 / 24h 使用同一上限。成交满 24
          小时后逐笔移出；下一笔释放不代表已足够恢复。手续费按成交金额 ×
          0.0125%；买卖按同批成交顺序配对，差价为（买入价 − 卖出价）×
          配对数量，在较晚成交时计入一次。负差价抵减成本，未配对部分先计手续费。
        </p>
      </details>
    </section>
  );
}
