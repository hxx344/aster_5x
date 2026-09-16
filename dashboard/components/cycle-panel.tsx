'use client';
import { Repeat2 } from 'lucide-react';
import {
  cycleActualLeverage,
  cycleCountdown,
  cycleHasPosition,
  cycleSpreadView,
  cycleStatus,
} from '@/lib/cycle';
import { cycleStateSummary } from '@/lib/cycle-events';
import { cycleMarginLimit, percentFromMarginLimit } from '@/lib/policy';
import {
  CycleSettings,
  type CycleSettingsProps,
} from '@/components/cycle-settings';
import { CycleVolumeSummary } from '@/components/cycle-volume-summary';
const dateTime = (value?: number) =>
  value && Number.isFinite(value)
    ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false })
    : '—';
type Props = Omit<CycleSettingsProps, 'stale'> & {
  dataTimestamp?: number;
  connectionError: string;
};
export function CyclePanel(props: Props) {
  const { account, now, dataTimestamp, connectionError } = props;
  const state = account.cycle_state;
  const view = cycleStatus(
    state,
    account.cycle?.enabled ?? false,
    account.enabled,
  );
  const stale =
    Boolean(connectionError) ||
    !dataTimestamp ||
    !Number.isFinite(now) ||
    now - dataTimestamp >= 8 ||
    now < dataTimestamp - 1;
  const ownedPosition = cycleHasPosition(state);
  const spread = cycleSpreadView(state, now, stale);
  const savedLeverage = cycleActualLeverage(
    account.snapshot,
    account.cycle?.symbol ?? 'XAUUSD1',
    now,
    stale,
  );
  const marginLimit = cycleMarginLimit(account.risk_limits);
  const marginLabel =
    marginLimit === null
      ? '待服务确认'
      : `${percentFromMarginLimit(marginLimit)}%`;
  return (
    <div className="feature-grid cycle-workspace">
      <div className="feature-stack">
        <section
          className="panel cycle-overview"
          aria-labelledby="cycle-heading"
        >
          <div className="section-head">
            <div>
              <h2 id="cycle-heading">
                <Repeat2 size={17} /> {account.cycle?.symbol ?? 'XAUUSD1'}
              </h2>
              <p>
                多空循环 ·{' '}
                {savedLeverage === null
                  ? '杠杆待核验'
                  : `实际 ${savedLeverage}x`}
              </p>
            </div>
            <span
              className={`migration-badge ${stale || ['attention', 'daily_limit'].includes(view.phase) ? 'amber' : ''}`}
            >
              {stale ? '最近记录 · ' : ''}
              {view.label}
            </span>
          </div>
          <div className="cycle-live-state" aria-live="polite">
            <p>{cycleStateSummary(state, view.phase, view.reason)}</p>
            {stale ? (
              <p className="amber">
                {connectionError ? '连接异常' : '状态数据已过期'}
                ，显示最近记录。
              </p>
            ) : null}
            {!account.enabled && (ownedPosition || state?.active_batch) ? (
              <p className="amber">
                暂停保留仓位和计时；当前批次继续核对，启动后按条件平仓。
              </p>
            ) : null}
          </div>
          <dl className="cycle-key-values">
            <div>
              <dt>检测价差 · bp</dt>
              <dd>{spread.value}</dd>
              <small className={spread.stale ? 'amber' : 'muted'}>
                {spread.notice}
                {spread.timestamp
                  ? ` · ${new Date(spread.timestamp * 1000).toLocaleTimeString('zh-CN', { hour12: false })}`
                  : ''}
              </small>
            </div>
            <div>
              <dt>最短持仓剩余</dt>
              <dd>{cycleCountdown(state, now, stale)}</dd>
              <small>到时仍需满足价差条件</small>
            </div>
            <div>
              <dt>已完成循环</dt>
              <dd>
                {state?.completed_cycles ?? 0}
                <span> 轮</span>
              </dd>
              <small>
                当前轮次：{ownedPosition ? '有新增仓位' : '无新增仓位'}
              </small>
            </div>
          </dl>
          <details className="disclosure inset">
            <summary>本轮仓位与时间</summary>{' '}
            <dl className="migration-details cycle-details">
              <div>
                <dt>已保存品种 / 当前实际杠杆</dt>
                <dd>
                  {account.cycle?.symbol ?? 'XAUUSD1'} /{' '}
                  {savedLeverage === null ? '待账户确认' : `${savedLeverage}x`}
                </dd>
              </div>
              <div>
                <dt>循环保证金上限</dt>
                <dd>
                  {marginLabel}
                  <small>
                    基础上限 + 5 个百分点，最高 100%；账户全部持仓共用。
                  </small>
                </dd>
              </div>
              <div>
                <dt>已完成循环</dt>
                <dd>{state?.completed_cycles ?? 0} 轮</dd>
              </div>
              <div>
                <dt>本轮新增多头数量</dt>
                <dd>{state?.quantities?.LONG ?? '—'}</dd>
              </div>
              <div>
                <dt>本轮新增空头数量</dt>
                <dd>{state?.quantities?.SHORT ?? '—'}</dd>
              </div>
              <div>
                <dt>本轮原始多头 / 空头</dt>
                <dd>
                  {state?.baseline?.LONG ?? '—'} /{' '}
                  {state?.baseline?.SHORT ?? '—'}
                </dd>
              </div>
              <div>
                <dt>最近检测深度价差 · bp</dt>
                <dd>
                  {spread.value}
                  <small className={spread.stale ? 'amber' : ''}>
                    {spread.notice}
                    {spread.timestamp
                      ? ` · ${new Date(spread.timestamp * 1000).toLocaleTimeString('zh-CN', { hour12: false })}`
                      : ''}
                  </small>
                </dd>
              </div>
              <div>
                <dt>最短持仓剩余</dt>
                <dd>{cycleCountdown(state, now, stale)}</dd>
              </div>
              <div>
                <dt>本轮开仓确认时间</dt>
                <dd>{dateTime(state?.opened_at)}</dd>
              </div>
              <div>
                <dt>最早允许平仓时间</dt>
                <dd>{dateTime(state?.close_eligible_at)}</dd>
              </div>
            </dl>
            <p className="migration-footnote">
              状态记录 {dateTime(state?.updated_at)} ·
              时间来自交易服务。到时后仍需满足价差条件。
            </p>
          </details>
        </section>
        <CycleVolumeSummary account={account} now={now} stale={stale} />
      </div>
      <CycleSettings {...props} stale={stale} />
    </div>
  );
}
