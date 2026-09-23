'use client';
import { Button } from '@/components/ui/button';
import {
  ConfigurationField,
  SettingsForm,
} from '@/components/configuration-fields';
import { draftFields } from '@/lib/use-account-draft';
import { Switch } from '@/components/ui/switch';
import { names, symbols } from '@/lib/desk-format';
import {
  CYCLE_MAXIMUM,
  cycleActualLeverage,
  cycleDraft,
  cycleHasPosition,
  parseCycleDraft,
  type CycleDraft,
} from '@/lib/cycle';
import type { FeatureProps } from '@/lib/desk-types';
import { accountConfigurationLock } from '@/lib/account-config';
const scopeOptions = [
  ['per_side', '每个方向分别计算'],
  ['gross', '多头与空头合计'],
] as const;
const holdUnits = [
  ['minutes', '分钟'],
  ['seconds', '秒'],
] as const;
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
  const fields = draftFields(draft, setDraft);
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
        <SettingsForm
          {...{ account, action, locked, clearDraft, setNotice, setError }}
          changes={() => {
            const cycle = parseCycleDraft(draft);
            if (cycle.enabled && account.migration?.enabled)
              throw new Error('请先停止 XAU 迁移，再开启多空循环');
            return { cycle };
          }}
          success="多空循环设置已保存，账户保持暂停；点击启动后开始执行"
          errorFallback="多空循环设置无效"
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
              <ConfigurationField
                id="cycle-symbol"
                label="循环品种"
                {...fields('symbol')}
                disabled={locked}
                display={draft.symbol}
                items={symbols.map((symbol) => [
                  symbol,
                  `${names[symbol]} · ${symbol}`,
                ])}
              />
              <div>
                <span>杠杆自动跟随</span>
                <p>
                  {leverage === null
                    ? '等待读取所选品种实际杠杆'
                    : `${leverage}x · 交易所当前杠杆`}
                </p>
              </div>
            </div>

            <ConfigurationField
              id="cycle-depth"
              label="深度价差参考金额"
              unit="USD1 / 每边"
              max={CYCLE_MAXIMUM.notional}
              {...fields('spread_notional')}
            />
            <ConfigurationField
              id="cycle-spread"
              label="开仓和平仓价差上限"
              unit="bp"
              max={CYCLE_MAXIMUM.spread_limit_bp}
              {...fields('spread_limit_bp')}
            />

            <ConfigurationField
              id="cycle-scope"
              label="名义价值范围口径"
              {...fields('notional_scope')}
              disabled={locked}
              display={
                draft.notional_scope === 'gross'
                  ? '多头与空头合计'
                  : '每个方向分别计算'
              }
              items={scopeOptions}
            />
            <div className="cycle-fields">
              <ConfigurationField
                id="cycle-min"
                label="名义价值下限"
                unit="USD1"
                max={CYCLE_MAXIMUM.notional}
                {...fields('min_notional')}
              />
              <ConfigurationField
                id="cycle-max"
                label="名义价值上限"
                unit="USD1"
                max={CYCLE_MAXIMUM.notional}
                {...fields('max_notional')}
              />
            </div>

            <ConfigurationField
              id="cycle-capacity-multiplier"
              label="开仓额度倍数"
              unit="1–100 倍，支持小数"
              min="1"
              max={CYCLE_MAXIMUM.capacity_multiplier}
              {...fields('capacity_multiplier')}
            />

            <div className="cycle-fields">
              <ConfigurationField
                id="cycle-hold"
                label="最短持仓时间"
                {...fields('hold_duration')}
              />
              <ConfigurationField
                id="cycle-hold-unit"
                label="时间单位"
                {...fields('hold_unit')}
                disabled={locked}
                display={draft.hold_unit === 'minutes' ? '分钟' : '秒'}
                items={holdUnits}
              />
            </div>

            <ConfigurationField
              id="cycle-daily-volume"
              label="成交额度上限"
              unit="USD1 · 0 为不限"
              max={CYCLE_MAXIMUM.daily_volume_limit}
              {...fields('daily_volume_limit')}
            />
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
        </SettingsForm>
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
            每个品种分别累计自己的 UTC 当日循环成交，各自使用所设每日上限。 近
            24 小时成交量仅作统计，不限制开仓。每边 10,000 USD1
            的一轮多空开仓和平仓，交易量约 40,000
            USD1；修复成交也计入，手续费不计入。当日剩余额度不足新一轮时等待日额度恢复，平仓不受额度限制。价格变化或修复可能使实际成交量超过上限。
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
