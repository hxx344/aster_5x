'use client';
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
  cycleActualLeverage,
  cycleDraft,
  cycleHasPosition,
  parseCycleDraft,
  type CycleDraft,
} from '@/lib/cycle';
import type { FeatureProps } from '@/lib/desk-types';
import { accountConfigurationLock } from '@/lib/account-config';
export type CycleSettingsProps = FeatureProps & {
  draft: CycleDraft;
  setDraft: (draft: CycleDraft) => void;
  clearDraft: () => void;
  now: number;
  stale: boolean;
};
export function CycleSettings({
  account,
  draft,
  setDraft,
  clearDraft,
  busy,
  now,
  stale,
  action,
  setError,
  setNotice,
}: CycleSettingsProps) {
  const state = account.cycle_state;
  const ownedPosition = cycleHasPosition(state);
  const configurationLock = accountConfigurationLock(account);
  const locked = busy || Boolean(configurationLock);
  const dirty =
    JSON.stringify(draft) !== JSON.stringify(cycleDraft(account.cycle));
  const leverage = cycleActualLeverage(
    account.snapshot,
    draft.symbol,
    now,
    stale,
  );
  const field = (key: keyof CycleDraft, value: string | boolean) =>
    setDraft({ ...draft, [key]: value });
  return (
    <section className="panel settings-panel cycle-settings">
      <div className="section-head">
        <h2>循环设置</h2>
        <span className="small-note">保存后仍保持暂停</span>
      </div>
      <dl className="saved-settings">
        <div>
          <dt>名义价值 · USD1</dt>
          <dd>
            {account.cycle?.min_notional ?? '—'} –{' '}
            {account.cycle?.max_notional ?? '—'}
            <small>
              {account.cycle?.notional_scope === 'gross'
                ? '多空合计'
                : '每个方向'}
            </small>
          </dd>
        </div>
        <div>
          <dt>价差上限</dt>
          <dd>{account.cycle?.spread_limit_bp ?? '—'} bp</dd>
        </div>
        <div>
          <dt>最短持仓</dt>
          <dd>{account.cycle?.hold_seconds ?? '—'} 秒</dd>
        </div>
        <div>
          <dt>开仓额度倍数</dt>
          <dd>{account.cycle?.capacity_multiplier ?? '1'} 倍</dd>
        </div>
      </dl>
      {locked ? (
        <p className="settings-lock muted">
          {configurationLock || '正在处理操作。'}
        </p>
      ) : null}
      <details className="disclosure inset">
        <summary>编辑循环参数{dirty ? ' · 未保存' : ''}</summary>
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
                setNotice(
                  '多空循环设置已保存，账户保持暂停；点击启动后开始执行',
                );
              }
            } catch (error) {
              setNotice('');
              setError(
                error instanceof Error ? error.message : '多空循环设置无效',
              );
            }
          }}
        >
          <fieldset disabled={locked} className="cycle-form-grid">
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
              <div>
                <span>杠杆自动跟随</span>
                <p>
                  {leverage === null
                    ? '等待读取所选品种实际杠杆'
                    : `${leverage}x · 交易所当前杠杆`}
                </p>
              </div>
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
                onChange={(event) =>
                  field('spread_notional', event.target.value)
                }
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
                onChange={(event) =>
                  field('spread_limit_bp', event.target.value)
                }
              />
            </label>

            <label htmlFor="cycle-scope">
              名义价值范围口径
              <Select
                value={draft.notional_scope}
                disabled={locked}
                onValueChange={(value) =>
                  value && field('notional_scope', value)
                }
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
                  onChange={(event) =>
                    field('min_notional', event.target.value)
                  }
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
                  onChange={(event) =>
                    field('max_notional', event.target.value)
                  }
                />
              </label>
            </div>

            <label htmlFor="cycle-capacity-multiplier">
              开仓额度倍数 <span>1–100 倍，支持小数</span>
              <Input
                id="cycle-capacity-multiplier"
                type="number"
                min="1"
                max="100"
                step="any"
                required
                value={draft.capacity_multiplier}
                onChange={(event) =>
                  field('capacity_multiplier', event.target.value)
                }
              />
            </label>

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
                  onChange={(event) =>
                    field('hold_duration', event.target.value)
                  }
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
          </fieldset>

          {ownedPosition ? (
            <p className="muted amber">
              本轮仍有循环仓位，请恢复运行完成平仓后，再暂停修改设置或退出循环模式。
            </p>
          ) : null}
          {dirty ? <p className="amber draft-notice">有未保存修改</p> : null}
          <div className="form-actions">
            <Button
              type="submit"
              variant="outline"
              className="full-width"
              disabled={locked}
            >
              保存循环设置
            </Button>
            <Button
              type="button"
              variant="ghost"
              disabled={busy || !dirty}
              onClick={clearDraft}
            >
              撤销修改
            </Button>
          </div>
        </form>
      </details>
      <details className="disclosure inset">
        <summary>循环规则与参数说明</summary>
        <div className="rules-copy">
          <p className="muted">
            可任选一个品种。已有仓位沿用持仓杠杆，空仓沿用交易所当前杠杆；循环不会修改杠杆。先加仓，确认后按持仓时长和价差条件减回本轮新增，核实原始多空数量不变后才开始下一轮。
          </p>{' '}
          <p className="muted">
            按参考金额分别计算买卖深度均价，价差 ≤ 阈值才执行。0.1 bp =
            0.001%。开仓和平仓使用同一阈值。
          </p>{' '}
          <p className="muted">
            {draft.notional_scope === 'per_side'
              ? '例如上限 10,000：多头和空头分别最多 10,000 USD1，合计最多 20,000 USD1。'
              : '例如上限 10,000：多头与空头名义价值合计最多 10,000 USD1。'}{' '}
            金额仅指本轮新增量。下单需扣除原仓占用后仍有当前杠杆额度，并满足可用保证金及循环保证金上限。
          </p>{' '}
          <p className="muted">
            先检查实际杠杆的公共余量 ≥ 本轮多空合计目标金额 ×
            倍数，再检查热差价；提交前再次核对。
            目标金额按上面的名义价值上限计算：每边 10,000 USD1、2
            倍，需要公共余量至少 40,000 USD1。
            减仓不检查额度，只减回本轮新增数量，保留原始多空持仓。
          </p>{' '}
          <p className="muted">
            从多空加仓成交均确认后计时，精确到整数秒，最长 7
            天。减回新增量并核实原始数量后自动开始下一轮。
          </p>{' '}
          <p className="muted">
            每个品种分别累计自己的 UTC 日与滚动 24
            小时循环成交，各自使用所设上限。每边 10,000 USD1
            的一轮多空开仓和平仓，交易量约 40,000
            USD1；修复成交也计入，手续费不计入。任一剩余额度不足新一轮时等待两项额度均足够，平仓不受额度限制。价格变化或修复可能使实际成交量超过上限。
          </p>{' '}
          <p className="muted">
            多空订单成批提交，成交不保证同一瞬间完成。循环品种禁止本账户普通加仓，其他已配置品种可同时运行普通加仓。循环与
            XAU
            迁移不可同时开启；更改设置需先暂停账户、核对完当前批次并减回本轮新增量。原有仓位可保留，保存不会启动账户。
          </p>
        </div>
      </details>
    </section>
  );
}
