'use client';

import { CirclePause, CirclePlay, Repeat2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  cycleCountdown,
  cycleDraft,
  cycleHasPosition,
  cycleSpreadView,
  cycleStatus,
  parseCycleDraft,
  type CycleConfig,
  type CycleDraft,
  type CycleState,
} from '@/lib/cycle';
import { cycleDailySummary, cycleRollingSummary } from '@/lib/cycle-daily';
import { CycleCostSummary } from '@/components/cycle-cost-summary';
import { CycleDiagnostic } from '@/components/cycle-diagnostic';
import { cycleMarginLimit, percentFromMarginLimit } from '@/lib/policy';

type CycleAccount = {
  id: string;
  name: string;
  mode: string;
  enabled: boolean;
  status: string;
  cycle?: CycleConfig;
  cycle_state?: CycleState;
  risk_limits?: { cycle?: string };
  migration?: { enabled: boolean };
  migration_state?: { active_batch?: object | null };
};

type Props = {
  account: CycleAccount;
  draft: CycleDraft;
  setDraft: (draft: CycleDraft) => void;
  clearDraft: () => void;
  busy: boolean;
  canStart: boolean;
  now: number;
  dataTimestamp?: number;
  connectionError: string;
  action: (
    url: string,
    body?: object,
    method?: 'POST' | 'PATCH',
  ) => Promise<boolean>;
  setError: (message: string) => void;
  setNotice: (message: string) => void;
};

const dateTime = (value?: number) =>
  value && Number.isFinite(value)
    ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false })
    : '—';

export function CyclePanel({
  account,
  draft,
  setDraft,
  clearDraft,
  busy,
  canStart,
  now,
  dataTimestamp,
  connectionError,
  action,
  setError,
  setNotice,
}: Props) {
  const state = account.cycle_state;
  const view = cycleStatus(
    state,
    account.cycle?.enabled ?? false,
    account.enabled,
  );
  const ownedPosition = cycleHasPosition(state);
  const pending = Boolean(
    state?.active_batch || account.migration_state?.active_batch,
  );
  const locked = busy || account.enabled || pending || ownedPosition;
  const dirty =
    JSON.stringify(draft) !== JSON.stringify(cycleDraft(account.cycle));
  const stale =
    Boolean(connectionError) ||
    !dataTimestamp ||
    !Number.isFinite(now) ||
    now - dataTimestamp >= 8 ||
    now < dataTimestamp - 1;
  const field = (key: keyof CycleDraft, value: string | boolean) =>
    setDraft({ ...draft, [key]: value });
  const spread = cycleSpreadView(state, now, stale);
  const daily = cycleDailySummary(state?.daily_volume, now, stale);
  const rolling = cycleRollingSummary(state?.rolling_volume, now, stale);
  const marginLimit = cycleMarginLimit(account.risk_limits);
  const marginLabel =
    marginLimit === null
      ? '待服务确认'
      : `${percentFromMarginLimit(marginLimit)}%`;
  const volumeWaiting =
    ['daily_limit', 'rolling_limit'].includes(view.phase) ||
    Boolean(state?.daily_volume?.reached || state?.rolling_volume?.reached);

  return (
    <section
      className="panel settings-panel cycle-panel"
      aria-labelledby="cycle-heading"
    >
      <div className="section-head">
        <h2 id="cycle-heading">
          <Repeat2 size={17} /> 多空循环
        </h2>
        <span
          className={`migration-badge ${view.phase === 'attention' || volumeWaiting || stale ? 'amber' : ''}`}
        >
          {stale ? '最近记录 · ' : ''}
          {view.label}
        </span>
      </div>
      <div className="migration-state cycle-state" aria-live="polite">
        <p className="cycle-account-name">当前账户 · {account.name}</p>
        <p>{view.reason}</p>
        {stale ? (
          <p className="amber">
            {connectionError ? '连接异常' : '状态数据已过期'}，以下为最近记录
          </p>
        ) : null}
        {!account.enabled && (ownedPosition || state?.active_batch) ? (
          <p className="amber">
            暂停会保留仓位和计时；当前批次继续核对，启动后再按条件平仓。
          </p>
        ) : null}
      </div>
      <CycleDiagnostic diagnostic={state?.diagnostic} />
      <section
        className="cycle-daily-summary"
        aria-labelledby="cycle-daily-heading"
      >
        <h3 id="cycle-daily-heading">
          成交额度 <span>UTC 日统计 · {daily.date}</span>
        </h3>
        <dl className="cycle-daily-grid">
          <div>
            <dt>已成交 · USD1</dt>
            <dd>{daily.volume}</dd>
          </div>
          <div>
            <dt>日 / 24h 共用上限 · USD1</dt>
            <dd>{daily.limit}</dd>
          </div>
          <div>
            <dt>剩余额度 · USD1</dt>
            <dd>{daily.remaining}</dd>
          </div>
          <div>
            <dt>当日成交笔数</dt>
            <dd>{daily.trades}</dd>
          </div>
        </dl>
        {daily.notice ? <p className="amber">{daily.notice}</p> : null}
        <CycleCostSummary
          label="UTC 当日成本"
          cost={state?.daily_volume?.cost}
          stale={stale || daily.rolloverPending}
        />
        <p>
          UTC 日额度重置：<time>{daily.resetAt}</time>
        </p>
        <h4 className="cycle-rolling-heading">滚动 24 小时统计</h4>
        <dl className="cycle-daily-grid cycle-rolling-grid">
          <div>
            <dt>近 24 小时已成交 · USD1</dt>
            <dd>{rolling.volume}</dd>
          </div>
          <div>
            <dt>近 24 小时剩余 · USD1</dt>
            <dd>{rolling.remaining}</dd>
          </div>
          <div>
            <dt>近 24 小时成交笔数</dt>
            <dd>{rolling.trades}</dd>
          </div>
        </dl>
        {rolling.notice ? <p className="amber">{rolling.notice}</p> : null}
        <CycleCostSummary
          label="近 24 小时成本"
          cost={state?.rolling_volume?.cost}
          stale={rolling.stale}
        />
        <p className="cycle-rolling-window">
          统计窗口：<time>{rolling.windowStart}</time>
          <span>至</span>
          <time>{rolling.windowEnd}</time>
        </p>
        <p>
          下一笔额度释放：<time>{rolling.releaseAt}</time>
        </p>
        {rolling.hasEstimates ? (
          <p className="amber">
            其中 {rolling.estimatedVolume} USD1 来自旧模拟单的估算成交时间。
          </p>
        ) : null}
        <p>成交满 24 小时后逐笔移出统计；下一笔释放不代表额度已足够恢复。</p>
        <p className="cycle-cost-note">
          手续费按每笔成交金额 × 0.0125%
          统计。差价按同批买卖成交的先后顺序配对， 用（买入价 − 卖出价）×
          配对数量计算，在较晚成交时计入对应 UTC 日与 24h
          窗口一次；负差价会抵减成本。未配对部分只先计手续费。
        </p>
        {account.cycle?.enabled ? (
          !account.enabled ? (
            <p>账户已手动暂停，UTC 换日或滚动额度释放后仍需手动启动。</p>
          ) : volumeWaiting ? (
            <p className="amber">
              {ownedPosition
                ? '继续本轮条件平仓，暂停新增。'
                : '成交额度已满或不足开启下一轮，暂停新增。'}
              UTC 日与滚动 24
              小时额度均足够，且交易条件满足后才自动恢复；点击暂停可取消自动恢复。
            </p>
          ) : (
            <p>
              任一额度不足时暂停新增；已有仓位仍按条件平仓。UTC 日与滚动 24
              小时额度均足够，且交易条件满足后才自动恢复。
            </p>
          )
        ) : null}
      </section>
      <dl className="migration-details cycle-details">
        <div>
          <dt>已保存品种 / 固定杠杆</dt>
          <dd>
            {account.cycle?.symbol ?? 'XAUUSD1'} /{' '}
            {account.cycle?.leverage ?? 2}x
          </dd>
        </div>
        <div>
          <dt>循环保证金上限</dt>
          <dd>
            {marginLabel}
            <small>基础上限 + 5 个百分点，最高 100%；账户全部持仓共用。</small>
          </dd>
        </div>
        <div>
          <dt>已完成循环</dt>
          <dd>{state?.completed_cycles ?? 0} 轮</dd>
        </div>
        <div>
          <dt>本轮多头数量</dt>
          <dd>{state?.quantities?.LONG ?? '—'}</dd>
        </div>
        <div>
          <dt>本轮空头数量</dt>
          <dd>{state?.quantities?.SHORT ?? '—'}</dd>
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
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          if (locked) return;
          try {
            const cycle = parseCycleDraft(draft);
            if (cycle.enabled && account.migration?.enabled)
              throw new Error('请先停止 XAU 迁移，再开启多空循环');
            if (
              await action(`/api/accounts/${account.id}`, { cycle }, 'PATCH')
            ) {
              clearDraft();
              setNotice('多空循环设置已保存，账户保持暂停；点击启动后开始执行');
            }
          } catch (error) {
            setNotice('');
            setError(
              error instanceof Error ? error.message : '多空循环设置无效',
            );
          }
        }}
      >
        <fieldset disabled={locked}>
          <div className="migration-toggle">
            <label htmlFor="cycle-enabled">使用独立多空循环</label>
            <Switch
              id="cycle-enabled"
              aria-label="使用独立多空循环"
              checked={draft.enabled}
              disabled={locked || Boolean(account.migration?.enabled)}
              onCheckedChange={(enabled) => field('enabled', enabled)}
            />
          </div>
          {account.migration?.enabled ? (
            <p className="muted amber">
              当前已启用 XAU 迁移，请先停止迁移后再选择循环。
            </p>
          ) : null}
          <div className="cycle-fields">
            <label htmlFor="cycle-symbol">
              循环品种
              <Select
                value={draft.symbol}
                disabled={locked}
                onValueChange={(value) => value && field('symbol', value)}
              >
                <SelectTrigger id="cycle-symbol" className="full-width">
                  <SelectValue>{draft.symbol}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="XAUUSD1">黄金 · XAUUSD1</SelectItem>
                  <SelectItem value="SPCXUSD1">SpaceX · SPCXUSD1</SelectItem>
                  <SelectItem value="CLUSD1">原油 · CLUSD1</SelectItem>
                </SelectContent>
              </Select>
            </label>
            <label htmlFor="cycle-leverage">
              固定杠杆 <span>x</span>
              <Input
                id="cycle-leverage"
                type="number"
                min="1"
                max="125"
                step="1"
                required
                value={draft.leverage}
                onChange={(event) => field('leverage', event.target.value)}
              />
            </label>
          </div>
          <label htmlFor="cycle-depth">
            深度价差参考金额 <span>USD1 / 每边</span>
            <Input
              id="cycle-depth"
              type="number"
              min="0"
              max="1000000"
              step="any"
              required
              value={draft.spread_notional}
              onChange={(event) => field('spread_notional', event.target.value)}
            />
          </label>
          <label htmlFor="cycle-spread">
            开仓和平仓价差上限 <span>bp</span>
            <Input
              id="cycle-spread"
              type="number"
              min="0"
              max="100"
              step="any"
              required
              value={draft.spread_limit_bp}
              onChange={(event) => field('spread_limit_bp', event.target.value)}
            />
          </label>
          <p className="muted">
            按参考金额分别计算买卖深度均价，价差 ≤ 阈值才执行。0.1 bp =
            0.001%。开仓和平仓使用同一阈值。
          </p>
          <label htmlFor="cycle-scope">
            名义价值范围口径
            <Select
              value={draft.notional_scope}
              disabled={locked}
              onValueChange={(value) => value && field('notional_scope', value)}
            >
              <SelectTrigger id="cycle-scope" className="full-width">
                <SelectValue>
                  {draft.notional_scope === 'gross'
                    ? '多头与空头合计'
                    : '每个方向分别计算'}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="per_side">每个方向分别计算</SelectItem>
                <SelectItem value="gross">多头与空头合计</SelectItem>
              </SelectContent>
            </Select>
          </label>
          <div className="cycle-fields">
            <label htmlFor="cycle-min">
              名义价值下限 <span>USD1</span>
              <Input
                id="cycle-min"
                type="number"
                min="0"
                max="1000000"
                step="any"
                required
                value={draft.min_notional}
                onChange={(event) => field('min_notional', event.target.value)}
              />
            </label>
            <label htmlFor="cycle-max">
              名义价值上限 <span>USD1</span>
              <Input
                id="cycle-max"
                type="number"
                min="0"
                max="1000000"
                step="any"
                required
                value={draft.max_notional}
                onChange={(event) => field('max_notional', event.target.value)}
              />
            </label>
          </div>
          <p className="muted">
            {draft.notional_scope === 'per_side'
              ? '例如上限 10,000：多头和空头分别最多 10,000 USD1，合计最多 20,000 USD1。'
              : '例如上限 10,000：多头与空头名义价值合计最多 10,000 USD1。'}{' '}
            下单还需满足交易规则、可用保证金及循环保证金上限；该上限由账户全部持仓共用。
          </p>
          <div className="cycle-fields">
            <label htmlFor="cycle-hold">
              最短持仓时间
              <Input
                id="cycle-hold"
                type="number"
                min="0"
                step="any"
                required
                value={draft.hold_duration}
                onChange={(event) => field('hold_duration', event.target.value)}
              />
            </label>
            <label htmlFor="cycle-hold-unit">
              时间单位
              <Select
                value={draft.hold_unit}
                disabled={locked}
                onValueChange={(value) => value && field('hold_unit', value)}
              >
                <SelectTrigger id="cycle-hold-unit" className="full-width">
                  <SelectValue>
                    {draft.hold_unit === 'minutes' ? '分钟' : '秒'}
                  </SelectValue>
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="minutes">分钟</SelectItem>
                  <SelectItem value="seconds">秒</SelectItem>
                </SelectContent>
              </Select>
            </label>
          </div>
          <p className="muted">
            从多空开仓成交均确认后计时，精确到整数秒，最长 7
            天。平仓均确认后自动开始下一轮。
          </p>
          <label htmlFor="cycle-daily-volume">
            成交额度上限 <span>USD1 · 0 为不限</span>
            <Input
              id="cycle-daily-volume"
              type="number"
              min="0"
              max="1000000000000"
              step="any"
              required
              value={draft.daily_volume_limit}
              onChange={(event) =>
                field('daily_volume_limit', event.target.value)
              }
            />
          </label>
          <p className="muted">
            UTC 日与滚动 24 小时分别累计全部循环成交，共用同一个上限。每边
            10,000 USD1 的一轮多空开仓和平仓，交易量约 40,000
            USD1；修复成交也计入，手续费不计入。任一剩余额度不足新一轮时等待两项额度均足够，平仓不受额度限制。价格变化或修复可能使实际成交量超过上限。
          </p>
        </fieldset>
        <p className="muted">
          多空订单成批提交，成交不保证同一瞬间完成。此模式独立于普通加仓与 XAU
          迁移；更改设置需先暂停、核对完当前批次并将循环仓位全部平仓。保存不会启动交易。
        </p>
        {ownedPosition ? (
          <p className="muted amber">
            本轮仍有循环仓位，请恢复运行完成平仓后，再暂停修改设置或退出循环模式。
          </p>
        ) : null}
        <Button
          type="submit"
          variant="outline"
          className="full-width"
          disabled={locked}
        >
          保存循环设置
        </Button>
      </form>
      <div className="execution-buttons cycle-buttons">
        <Button
          disabled={busy || !canStart || !account.cycle?.enabled || dirty}
          title={dirty ? '请先保存循环设置' : '按已保存的循环设置启动账户'}
          onClick={() => void action(`/api/accounts/${account.id}/enable`)}
        >
          <CirclePlay size={16} />
          {account.mode === 'paper' ? '启动模拟循环' : '启动实盘循环'}
        </Button>
        <Button
          variant="outline"
          disabled={busy || !account.enabled || !account.cycle?.enabled}
          onClick={() => void action(`/api/accounts/${account.id}/pause`)}
        >
          <CirclePause size={16} />
          暂停循环
        </Button>
      </div>
      {account.cycle?.enabled && account.status === 'attention' ? (
        <Button
          className="reconcile-button"
          variant="outline"
          disabled={busy}
          onClick={() => void action(`/api/accounts/${account.id}/retry`)}
        >
          核对循环未完成批次
        </Button>
      ) : null}
    </section>
  );
}
