'use client';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import type { FeatureProps } from '@/lib/desk-types';
import { useAccountDraft } from '@/lib/use-account-draft';
import {
  Layers3,
  ShieldCheck,
  ArrowUpRight,
  ArrowDownLeft,
} from 'lucide-react';
import { Progress } from '@/components/ui/progress';
import { fmt, pct, clock } from '@/lib/desk-format';
import { accountModeView, accountSnapshotView } from '@/lib/account-modes';
import { accountConfigurationLock } from '@/lib/account-config';
import { DeleteAccountDialog } from '@/components/delete-account-dialog';
import {
  cycleMarginLimit,
  marginLimitFromPercent,
  migrationMarginLimit,
  percentFromMarginLimit,
} from '@/lib/policy';
export function AccountOverview({
  account,
  busy,
  action,
  setError,
  setNotice,
  now,
  connectionError,
}: FeatureProps & { now: number; connectionError: string }) {
  const snapshot = account.snapshot;
  const configurationLock = accountConfigurationLock(account);
  const locked = busy || Boolean(configurationLock);
  const positions =
    snapshot?.positions.filter((p) => Number(p.qty) !== 0) ?? [];
  const ratio = snapshot ? Number(snapshot.ratio ?? 1) : 0;
  const modeView = accountModeView(snapshot, now, connectionError);
  const snapshotView = accountSnapshotView(
    snapshot,
    now,
    connectionError,
    account.snapshot_refresh,
    account.status === 'error' ? account.reason : undefined,
  );
  const marginLimit = Number(account?.policy.margin_limit ?? '0.5');
  const marginPercent = percentFromMarginLimit(
    account?.policy.margin_limit ?? '0.5',
  );
  const highMarginLimitValue =
    account?.risk_limits?.high_leverage ??
    account?.policy.margin_limit ??
    '0.5';
  const highMarginLimit = Number(highMarginLimitValue);
  const highMarginPercent = percentFromMarginLimit(highMarginLimitValue);
  const migrationMarginPercent = percentFromMarginLimit(
    migrationMarginLimit(
      account?.policy.margin_limit ?? '0.5',
      account?.risk_limits,
    ),
  );
  const cycleMarginLimitValue = cycleMarginLimit(account?.risk_limits);
  const cycleRiskLimit =
    cycleMarginLimitValue === null ? null : Number(cycleMarginLimitValue);
  const cycleMarginPercent =
    cycleMarginLimitValue === null
      ? null
      : percentFromMarginLimit(cycleMarginLimitValue);
  const cycleMode = Boolean(account?.cycle?.enabled);
  const upperRiskLimit = cycleMode ? cycleRiskLimit : highMarginLimit;
  const upperRiskPercent = cycleMode ? cycleMarginPercent : highMarginPercent;
  const riskAccent =
    upperRiskLimit === null
      ? 'muted'
      : ratio > upperRiskLimit
        ? 'danger'
        : ratio > marginLimit
          ? 'amber'
          : 'mint';

  const {
    value: riskDraft,
    setValue: setRiskDraft,
    clear,
  } = useAccountDraft(account.id, marginPercent);
  return (
    <div className="feature-stack">
      {' '}
      <section className="metrics">
        <Metric
          label="总名义持仓金额"
          value={fmt(snapshot?.total_notional)}
          sub="USD1 · 全部仓位按标记价格计，多空累加"
          icon={<Layers3 size={17} />}
        />
        <Metric
          label="账户保证金比率"
          value={pct(snapshot?.margin_ratio)}
          sub="维持保证金 ÷ 账户总权益"
          icon={<ShieldCheck size={17} />}
        />
        <Metric
          label="未实现盈亏"
          value={fmt(snapshot?.unrealized)}
          sub={snapshot ? `${positions.length} 个方向持仓` : '等待账户仓位数据'}
          accent={Number(snapshot?.unrealized) < 0 ? 'danger' : 'mint'}
          icon={<ArrowUpRight size={17} />}
        />
      </section>
      <div className="feature-grid">
        <div className="panel positions-panel">
          <div className="section-head">
            <h2>当前持仓 · {snapshot ? positions.length : '—'}</h2>
            <span
              className={
                snapshotView.warning ? 'small-note amber' : 'small-note'
              }
              title={snapshotView.notice}
            >
              {snapshotView.label} {clock(snapshot?.timestamp)}
            </span>
          </div>
          <div className="table-scroll">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>市场 / 方向</TableHead>
                  <TableHead className="number">数量</TableHead>
                  <TableHead className="number">开仓均价</TableHead>
                  <TableHead className="number">标记价格</TableHead>
                  <TableHead className="number">杠杆</TableHead>
                  <TableHead className="number">名义价值</TableHead>
                  <TableHead className="number">占用保证金</TableHead>
                  <TableHead className="number">预估强平价</TableHead>
                  <TableHead className="number">未实现盈亏</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {positions.map((p) => (
                  <TableRow key={`${p.symbol}-${p.side}`}>
                    <TableCell>
                      <strong>{p.symbol}</strong>
                      <span
                        className={`direction ${p.side === 'LONG' ? 'mint' : 'danger'}`}
                      >
                        {p.side === 'LONG' ? (
                          <ArrowUpRight size={14} />
                        ) : (
                          <ArrowDownLeft size={14} />
                        )}{' '}
                        {p.side === 'LONG' ? '多仓' : '空仓'}
                      </span>
                    </TableCell>
                    <TableCell className="number">{fmt(p.qty, 4)}</TableCell>
                    <TableCell className="number">{fmt(p.entry)}</TableCell>
                    <TableCell className="number">{fmt(p.mark)}</TableCell>
                    <TableCell className="number">{p.leverage}x</TableCell>
                    <TableCell className="number">{fmt(p.notional)}</TableCell>
                    <TableCell className="number">
                      {fmt(p.occupied_margin)}
                    </TableCell>
                    <TableCell className="number">
                      {Number(p.liquidation) > 0 ? fmt(p.liquidation) : '—'}
                    </TableCell>
                    <TableCell
                      className={`number ${Number(p.unrealized) < 0 ? 'danger' : 'mint'}`}
                    >
                      {fmt(p.unrealized)}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
          {!positions.length && (
            <div className="empty-state">
              <Layers3 size={27} />
              <h3>{snapshotView.emptyTitle}</h3>
              <p>{snapshotView.emptyNotice}</p>
            </div>
          )}
        </div>
        <div className="feature-stack">
          {' '}
          <section className="panel risk-panel">
            <div className="section-head">
              <h2>风险约束</h2>
              <ShieldCheck className="mint" size={18} />
            </div>
            <div className="risk-value">
              <span className={riskAccent}>
                {snapshot ? pct(snapshot.ratio) : '—'}
              </span>
              <small>
                {cycleMode
                  ? cycleMarginPercent === null
                    ? '循环上限待服务确认'
                    : `循环 ≤ ${cycleMarginPercent}%`
                  : `普通 5x ≤ ${marginPercent}%`}
              </small>
            </div>
            <div className="risk-track">
              <Progress
                value={Math.min(100, ratio * 100)}
                aria-label="当前保证金占用率"
              />
              <i
                className="limit-marker"
                style={{ left: `${marginPercent}%` }}
                title={`普通 5x 基础上限 ${marginPercent}%`}
              />
              {upperRiskLimit !== null && upperRiskLimit > marginLimit && (
                <i
                  className="limit-marker high-limit-marker"
                  style={{ left: `${upperRiskPercent}%` }}
                  title={`${cycleMode ? '循环全部持仓' : '普通 10x / 20x'}上限 ${upperRiskPercent}%`}
                />
              )}
            </div>
            <div className="scale">
              <span>0%</span>
              <span>50%</span>
              <span>100%</span>
            </div>
            <details className="disclosure inset">
              <summary>各功能风险上限与计算口径</summary>
              <div className="risk-explanation muted">
                <p className="risk-bonus">
                  <span>普通 10x / 20x 加仓上限</span>
                  <strong>{highMarginPercent}%</strong>
                </p>
                <p className="risk-bonus">
                  <span>迁移上限 · 含 5x</span>
                  <strong>{migrationMarginPercent}%</strong>
                </p>
                <p className="risk-bonus">
                  <span>循环上限 · 全部持仓</span>
                  <strong>
                    {cycleMarginPercent === null
                      ? '待服务确认'
                      : `${cycleMarginPercent}%`}
                  </strong>
                </p>
                <p>共用额外 5 个百分点，最高 100%。</p>
                <p>每仓占用 = |数量| × 标记价格 ÷ 实际杠杆；多空分别累加。</p>
              </div>
            </details>
            <dl className="details">
              <div>
                <dt>总占用保证金 · USD1</dt>
                <dd>{fmt(snapshot?.occupied_margin)}</dd>
              </div>
              {modeView.rows.map((mode) => (
                <div key={mode.key}>
                  <dt>{mode.label}</dt>
                  <dd className={mode.failed ? 'danger' : ''}>{mode.value}</dd>
                </div>
              ))}
            </dl>
            <div className="mode-check-note">
              <p title="模式结果来自这次账户快照">
                {modeView.recordedAt
                  ? `账户快照 ${modeView.recordedAt}${modeView.elapsed ? ` · ${modeView.elapsed}` : ''}`
                  : modeView.hasSnapshot
                    ? '账户快照时间未知'
                    : '等待首次账户核验'}
              </p>
              {snapshotView.notice && (
                <p className={snapshotView.warning ? 'amber' : ''}>
                  {snapshotView.notice}
                </p>
              )}
            </div>
            <div className="risk-caption">
              三项账户模式必须满足，仅核验，不自动修改。下单前与成交后均检查风险。
            </div>
          </section>
          <details className="disclosure panel settings-panel">
            <summary>账户基础风险设置</summary>
            <form
              onSubmit={async (event) => {
                event.preventDefault();
                if (locked) return;
                try {
                  if (
                    await action(
                      `/api/accounts/${account.id}`,
                      { margin_limit: marginLimitFromPercent(riskDraft) },
                      'PATCH',
                    )
                  ) {
                    clear();
                    setNotice('账户风险上限已保存');
                  }
                } catch (error) {
                  setError(
                    error instanceof Error ? error.message : '风险上限无效',
                  );
                }
              }}
            >
              <label htmlFor="margin-percent">
                基础保证金占用上限 · %
                <Input
                  id="margin-percent"
                  type="number"
                  min="0"
                  max="100"
                  step="any"
                  required
                  disabled={locked}
                  value={riskDraft}
                  onChange={(event) => setRiskDraft(event.target.value)}
                />
              </label>
              <p className="muted">
                此设置由普通开仓、循环和迁移共用。
                {configurationLock || '各功能适用上限见上方风险约束。'}
              </p>
              <Button type="submit" variant="outline" disabled={locked}>
                保存账户风险上限
              </Button>
            </form>
          </details>
        </div>
      </div>
      <DeleteAccountDialog
        key={account.id}
        account={account}
        busy={busy}
        action={action}
        setNotice={setNotice}
        connectionError={connectionError}
      />
    </div>
  );
}
function Metric({
  label,
  value,
  sub,
  icon,
  accent = '',
}: {
  label: string;
  value: string;
  sub: string;
  icon: React.ReactNode;
  accent?: string;
}) {
  return (
    <article className="metric">
      <div className="metric-label">
        {label}
        {icon}
      </div>
      <div className={`metric-value ${accent}`}>{value}</div>
      <p>{sub}</p>
    </article>
  );
}
