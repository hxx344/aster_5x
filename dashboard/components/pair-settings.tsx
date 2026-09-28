'use client';
import { useId } from 'react';
import { ConfigurationField } from '@/components/configuration-fields';
import { Button } from '@/components/ui/button';
import { Switch } from '@/components/ui/switch';
import type { Account } from '@/lib/desk-types';
import { availablePairAccounts, type Pair, type PairDraft } from '@/lib/pairs';

export function PairSettings({
  draft,
  setDraft,
  accounts,
  pairs,
  editing,
  locked,
  submit,
  cancel,
}: {
  draft: PairDraft;
  setDraft: (draft: PairDraft) => void;
  accounts: Account[];
  pairs: Pair[];
  editing: boolean;
  locked: boolean;
  submit: () => Promise<void>;
  cancel: () => void;
}) {
  const formId = useId();
  const field = <K extends keyof PairDraft>(key: K, value: PairDraft[K]) =>
    setDraft({ ...draft, [key]: value });
  const textField = (
    key: Exclude<keyof PairDraft, 'cycle' | 'margin_enabled'>,
  ) => ({
    value: draft[key],
    onValueChange: (value: string) => setDraft({ ...draft, [key]: value }),
  });
  const cycleField = (key: keyof PairDraft['cycle']) => ({
    value: String(draft.cycle[key]),
    onValueChange: (value: string) =>
      field('cycle', { ...draft.cycle, [key]: value }),
  });
  const eligible = availablePairAccounts(
    accounts,
    pairs,
    editing ? draft.id : undefined,
  );
  const long = accounts.find((account) => account.id === draft.long_account_id);
  const short = accounts.find(
    (account) => account.id === draft.short_account_id,
  );
  const choices = (side: 'long' | 'short'): [string, string][] =>
    eligible
      .filter((account) => {
        const other = side === 'long' ? short : long;
        return (
          account.id !== other?.id && (!other || other.mode === account.mode)
        );
      })
      .map((account) => [
        account.id,
        `${account.name} · ${account.mode === 'live' ? '实盘' : '模拟'}`,
      ]);
  return (
    <form
      className="pair-settings-form form-stack"
      onSubmit={(event) => {
        event.preventDefault();
        if (!locked) void submit();
      }}
    >
      <fieldset disabled={locked} className="cycle-form-grid">
        <ConfigurationField
          id={`${formId}-pair-name`}
          label="配对组名称"
          type="text"
          maxLength={50}
          {...textField('name')}
          disabled={locked}
        />
        <ConfigurationField
          id={`${formId}-pair-id`}
          label="组标识"
          type="text"
          pattern="[a-z0-9_\-]{1,32}"
          {...textField('id')}
          disabled={locked || editing}
        />
        <ConfigurationField
          id={`${formId}-pair-long`}
          label="A · 只持多仓"
          items={choices('long')}
          {...textField('long_account_id')}
          display={long?.name}
          disabled={locked || editing}
          placeholder="选择子账户 A"
        />
        <ConfigurationField
          id={`${formId}-pair-short`}
          label="B · 只持空仓"
          items={choices('short')}
          {...textField('short_account_id')}
          display={short?.name}
          disabled={locked || editing}
          placeholder="选择子账户 B"
        />
        <p className="muted">
          固定 XAUUSD1。A 与 B 须属于同一主账户，方向固定。保留交易所 Hedge
          Mode，仅核验；旧反向仓位需先处理。
        </p>
        <ConfigurationField
          id={`${formId}-pair-mode`}
          label="组执行模式"
          value={draft.mode}
          display={
            {
              monitor: '保证金管理（不开仓）',
              ordinary: '普通市价共同开仓',
              cycle: '两子账户多空循环',
            }[draft.mode]
          }
          disabled={locked}
          onValueChange={(value) => field('mode', value as PairDraft['mode'])}
          items={[
            ['monitor', '保证金管理（不开仓）'],
            ['ordinary', '普通市价共同开仓'],
            ['cycle', '两子账户多空循环'],
          ]}
        />
      </fieldset>
      {draft.mode === 'ordinary' ? (
        <fieldset disabled={locked} className="cycle-form-grid">
          <legend>普通开仓</legend>
          <ConfigurationField
            id={`${formId}-pair-threshold`}
            label="公开额度门槛"
            unit="USD1"
            max="1000000000"
            {...textField('threshold')}
            disabled={locked}
          >
            <small>
              当前杠杆的市场公开额度须严格大于此值；两侧账户余量和保证金分别核验。
            </small>
          </ConfigurationField>
          <ConfigurationField
            id={`${formId}-pair-order`}
            label="每侧单批名义金额上限"
            unit="USD1"
            min="500"
            max="1000000"
            {...textField('order_notional')}
            disabled={locked}
          />
          <ConfigurationField
            id={`${formId}-pair-leverage`}
            label="最低开仓杠杆"
            display={`${draft.min_open_leverage}x`}
            items={[
              ['5', '5x'],
              ['10', '10x'],
              ['20', '20x'],
            ]}
            {...textField('min_open_leverage')}
            disabled={locked}
          >
            <small>
              这是普通新增开仓的最低门槛，不会在保存时修改交易所杠杆。组启用后，程序在更高的
              5x / 10x / 20x
              档位公开额度超过门槛、两侧持仓及账户检查通过时尝试共同升杠杆；确认两侧一致后再检查开仓条件。
            </small>
          </ConfigurationField>
          <ConfigurationField
            id={`${formId}-pair-spread`}
            label="最优买卖价差比例上限"
            max="0.0005"
            {...textField('spread_limit')}
            disabled={locked}
          >
            <small>0.0005 = 0.05% = 5 bp</small>
          </ConfigurationField>
        </fieldset>
      ) : null}
      {draft.mode === 'cycle' ? (
        <fieldset disabled={locked} className="cycle-form-grid">
          <legend>两侧共同循环</legend>
          <ConfigurationField
            id={`${formId}-pair-cycle-min`}
            label="名义价值下限"
            unit="USD1"
            {...cycleField('min_notional')}
            disabled={locked}
          />
          <ConfigurationField
            id={`${formId}-pair-cycle-max`}
            label="名义价值上限"
            unit="USD1"
            {...cycleField('max_notional')}
            disabled={locked}
          />
          <ConfigurationField
            id={`${formId}-pair-cycle-scope`}
            label="名义金额口径"
            display={
              draft.cycle.notional_scope === 'gross'
                ? '两侧合计'
                : '每侧分别计算'
            }
            items={[
              ['per_side', '每侧分别计算'],
              ['gross', '两侧合计'],
            ]}
            {...cycleField('notional_scope')}
            disabled={locked}
          />
          <ConfigurationField
            id={`${formId}-pair-cycle-multiplier`}
            label="公开额度门槛倍数"
            {...cycleField('capacity_multiplier')}
            disabled={locked}
          />
          <ConfigurationField
            id={`${formId}-pair-cycle-depth`}
            label="深度价差参考金额"
            unit="USD1 / 每侧"
            {...cycleField('spread_notional')}
            disabled={locked}
          />
          <ConfigurationField
            id={`${formId}-pair-cycle-spread`}
            label="深度价差上限"
            unit="bp"
            {...cycleField('spread_limit_bp')}
            disabled={locked}
          />
          <ConfigurationField
            id={`${formId}-pair-cycle-hold`}
            label="最短持仓时间"
            {...cycleField('hold_duration')}
            disabled={locked}
          />
          <ConfigurationField
            id={`${formId}-pair-cycle-unit`}
            label="持仓时间单位"
            display={draft.cycle.hold_unit === 'seconds' ? '秒' : '分钟'}
            items={[
              ['seconds', '秒'],
              ['minutes', '分钟'],
            ]}
            {...cycleField('hold_unit')}
            disabled={locked}
          />
          <ConfigurationField
            id={`${formId}-pair-cycle-daily`}
            label="每侧 UTC 当日成交量上限"
            unit="USD1 · 0 为不限制"
            {...cycleField('daily_volume_limit')}
            disabled={locked}
          >
            <small>
              两侧分别累计本组普通、循环和风险恢复成交；此上限限制新循环开仓，减回已有本轮仓位不受限制。
            </small>
          </ConfigurationField>
          <p className="muted">
            杠杆按两侧账户的实际设置核验；两侧须一致。每轮减回各自基线，不减原有普通底仓。
          </p>
        </fieldset>
      ) : null}
      <ConfigurationField
        id={`${formId}-pair-margin-limit`}
        label="每侧基础保证金占用上限"
        unit="%"
        max="100"
        {...textField('margin_percent')}
        disabled={locked}
      >
        <small>
          普通 5x 使用基础上限；普通 10x / 20x 与循环额外加 5 个百分点，最高
          100%。
        </small>
      </ConfigurationField>
      <fieldset disabled={locked} className="cycle-form-grid">
        <legend>主账户核验与自动保证金平衡</legend>
        <div className="migration-toggle">
          <label htmlFor={`${formId}-pair-margin-enabled`}>
            启用两侧可用余额平衡
          </label>
          <Switch
            id={`${formId}-pair-margin-enabled`}
            checked={draft.margin_enabled}
            disabled={locked}
            onCheckedChange={(enabled) => field('margin_enabled', enabled)}
          />
        </div>
        <ConfigurationField
          id={`${formId}-pair-master-prefix`}
          label="主账户凭据环境变量前缀"
          type="text"
          required={long?.mode === 'live'}
          placeholder="ASTER_MASTER"
          {...textField('master_env_prefix')}
          disabled={locked}
        >
          <small>
            实盘组必填独立主账户前缀，用于只读核验两子账户的共同归属；开启自动平衡后也用于划转。
          </small>
        </ConfigurationField>
        <ConfigurationField
          id={`${formId}-pair-balance-threshold`}
          label="触发可用余额差额"
          unit="USD1"
          max="1000000000"
          {...textField('balance_threshold')}
          disabled={locked}
        />
        <ConfigurationField
          id={`${formId}-pair-min-transfer`}
          label="最小划转金额"
          unit="USD1"
          min="0.00000001"
          max="1000000000"
          {...textField('min_transfer')}
          disabled={locked}
        />
        <ConfigurationField
          id={`${formId}-pair-max-transfer`}
          label="单次最大划转"
          unit="USD1"
          min="0.00000001"
          max="1000000000"
          {...textField('max_transfer')}
          disabled={locked}
        />
        <ConfigurationField
          id={`${formId}-pair-buffer`}
          label="转出侧风险与现金缓冲"
          unit="百分点 / %"
          max="100"
          {...textField('buffer_percent')}
          disabled={locked}
        >
          <small>
            占用上限减去所填百分点，同时保留不少于转出侧当前权益 ×
            此比例的可用余额。
          </small>
        </ConfigurationField>
        <ConfigurationField
          id={`${formId}-pair-check`}
          label="平衡检查间隔"
          unit="秒"
          step="1"
          min="1"
          max="3600"
          {...textField('check_interval_seconds')}
          disabled={locked}
        />
        <ConfigurationField
          id={`${formId}-pair-cooldown`}
          label="划转冷却时间"
          unit="秒"
          step="1"
          min="1"
          max="86400"
          {...textField('cooldown_seconds')}
          disabled={locked}
        />
        <p className="muted">
          只填写凭据前缀，密钥由服务器读取。组启用且自动平衡开启时，包括「保证金管理（不开仓）」模式在内，均可触发划转。以两侧可用余额差的一半为平衡目标，并受可划金额、单次限额及风险与现金缓冲约束。未知结果只读核对，缺少可靠结果时保留待确认状态，不重复划转。
        </p>
      </fieldset>
      <div className="form-actions">
        <Button type="submit" disabled={locked}>
          {editing ? '保存设置，保持暂停' : '创建配对组，保持暂停'}
        </Button>
        <Button
          type="button"
          variant="outline"
          disabled={locked}
          onClick={cancel}
        >
          撤销修改
        </Button>
      </div>
    </form>
  );
}
