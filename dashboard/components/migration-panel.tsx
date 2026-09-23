'use client';
import { Button } from '@/components/ui/button';
import {
  ConfigurationField,
  SettingsForm,
} from '@/components/configuration-fields';
import type { FeatureProps } from '@/lib/desk-types';
import { draftFields, useAccountDraft } from '@/lib/use-account-draft';
import { Switch } from '@/components/ui/switch';
import { fmt } from '@/lib/desk-format';
import { accountConfigurationLock } from '@/lib/account-config';
import { migrationStatus, type MigrationDraft } from '@/lib/migration';
import {
  migrationMarginLimit,
  migrationToleranceFromPercent,
  percentFromMarginLimit,
} from '@/lib/policy';
export function MigrationPanel({
  account,
  busy,
  action,
  setError,
  setNotice,
  connectionError,
}: FeatureProps & { connectionError: string }) {
  const snapshot = account.snapshot;
  const configurationLock = accountConfigurationLock(account);
  const locked = busy || Boolean(configurationLock);
  const migration = account.migration_state;
  const migrationView = migrationStatus(
    migration,
    account.migration?.enabled ?? false,
    account.enabled,
  );
  const migrationMarginPercent = percentFromMarginLimit(
    migrationMarginLimit(account.policy.margin_limit, account.risk_limits),
  );
  const {
    value: migrationForm,
    setValue: setMigrationForm,
    clear: clearDraft,
  } = useAccountDraft<MigrationDraft>(account.id, {
    enabled: account.migration?.enabled ?? false,
    spread_limit_bp: account.migration?.spread_limit_bp ?? '5',
    batch_notional: account.migration?.batch_notional ?? '1000',
    tolerance_percent: percentFromMarginLimit(
      account.migration?.notional_tolerance ?? '0.05',
    ),
  });
  const fields = draftFields(migrationForm, setMigrationForm);
  return (
    <section className="panel settings-panel migration-panel">
      <div className="section-head">
        <h2>XAU 仓位迁移</h2>
        <span
          className={`migration-badge ${migrationView.phase === 'attention' || migrationView.phase === 'residual' ? 'amber' : ''}`}
        >
          {migrationView.label}
        </span>
      </div>
      <div className="migration-state" aria-live="polite">
        {connectionError ? (
          <p className="amber">连接中断，以下为最近记录</p>
        ) : null}
        <p>{migrationView.reason}</p>
        {account.cycle?.enabled ? (
          <p className="amber">
            当前已开启多空循环，完成本轮并退出循环后才能启用迁移。
          </p>
        ) : null}
        {migration?.active_batch && !account?.enabled ? (
          <p className="amber">已停止开始新批次，正在核对当前批次。</p>
        ) : null}
      </div>
      {account?.migration?.enabled ? (
        <Button
          variant="outline"
          className="reconcile-button"
          disabled={busy}
          onClick={async () => {
            if (
              await action(
                `/api/accounts/${account.id}`,
                { migration: { enabled: false } },
                'PATCH',
              )
            ) {
              clearDraft();
              setNotice('迁移已停止，账户保持暂停');
            }
          }}
        >
          停止迁移并暂停账户
        </Button>
      ) : null}
      <dl className="saved-settings">
        <div>
          <dt>当前目标</dt>
          <dd>
            {migration?.active_batch?.target_symbol ||
              migration?.target_symbol ||
              '等待选择'}
          </dd>
        </div>
        <div>
          <dt>已完成批次</dt>
          <dd>{migration?.completed_batches ?? 0}</dd>
        </div>
        <div>
          <dt>单批每边上限 · USD1</dt>
          <dd>{account.migration?.batch_notional ?? '1000'}</dd>
        </div>
        <div>
          <dt>目标价差上限</dt>
          <dd>{account.migration?.spread_limit_bp ?? '5'} bp</dd>
        </div>
      </dl>
      {configurationLock ? (
        <p className="settings-lock muted">{configurationLock}</p>
      ) : null}
      <details className="disclosure inset">
        <summary>迁移数量与累计结果</summary>
        <dl className="migration-details">
          <div>
            <dt>XAU 剩余多头数量</dt>
            <dd>
              {migration?.source_remaining_qty?.LONG ??
                snapshot?.positions.find(
                  (p) => p.symbol === 'XAUUSD1' && p.side === 'LONG',
                )?.qty ??
                '—'}
            </dd>
          </div>
          <div>
            <dt>XAU 剩余空头数量</dt>
            <dd>
              {migration?.source_remaining_qty?.SHORT ??
                snapshot?.positions.find(
                  (p) => p.symbol === 'XAUUSD1' && p.side === 'SHORT',
                )?.qty ??
                '—'}
            </dd>
          </div>
          <div>
            <dt>当前目标</dt>
            <dd>
              {migration?.active_batch?.target_symbol ||
                migration?.target_symbol ||
                '—'}
            </dd>
          </div>
          <div>
            <dt>目标 / 要求杠杆</dt>
            <dd>
              {migration?.target_leverage
                ? `${migration.target_leverage}x`
                : '—'}{' '}
              /{' '}
              {migration?.required_leverage
                ? `≥ ${migration.required_leverage}x`
                : '—'}
            </dd>
          </div>
          <div>
            <dt>临时迁移占用上限</dt>
            <dd>{migrationMarginPercent}%</dd>
          </div>
          <div>
            <dt>适用迁移杠杆</dt>
            <dd>5x / 10x / 20x</dd>
          </div>
          <div>
            <dt>多头累计已迁 · USD1</dt>
            <dd>{fmt(migration?.migrated_notional?.LONG)}</dd>
          </div>
          <div>
            <dt>空头累计已迁 · USD1</dt>
            <dd>{fmt(migration?.migrated_notional?.SHORT)}</dd>
          </div>
          <div>
            <dt>多头累计金额差 · USD1</dt>
            <dd>{fmt(migration?.cumulative_notional_delta?.LONG)}</dd>
          </div>
          <div>
            <dt>空头累计金额差 · USD1</dt>
            <dd>{fmt(migration?.cumulative_notional_delta?.SHORT)}</dd>
          </div>
        </dl>
        <p className="migration-footnote">
          已完成 {migration?.completed_batches ?? 0} 批 · 金额差为目标开仓减去
          XAU 平仓，按多空分别累计。
        </p>
      </details>
      <details className="disclosure inset">
        <summary>迁移设置</summary>
        <SettingsForm
          {...{ account, action, locked, clearDraft, setNotice, setError }}
          changes={() => ({
            migration: {
              enabled: migrationForm.enabled,
              spread_limit_bp: migrationForm.spread_limit_bp,
              batch_notional: migrationForm.batch_notional,
              notional_tolerance: migrationToleranceFromPercent(
                migrationForm.tolerance_percent,
              ),
            },
          })}
          success="迁移设置已保存，启动账户后按设置运行"
          errorFallback="迁移设置无效"
        >
          <div className="migration-toggle">
            <label htmlFor="migration-enabled">允许迁移 XAU 仓位</label>
            <Switch
              id="migration-enabled"
              aria-label="允许迁移 XAU 仓位"
              disabled={locked || !!account.cycle?.enabled}
              checked={migrationForm.enabled}
              onCheckedChange={(enabled) =>
                setMigrationForm({ ...migrationForm, enabled })
              }
            />
          </div>
          <ConfigurationField
            id="migration-spread"
            label="目标深度价差上限"
            unit="bp"
            max="100"
            disabled={locked}
            {...fields('spread_limit_bp')}
          />
          <ConfigurationField
            id="migration-batch"
            label="单批每边上限"
            unit="USD1"
            min="500"
            max="1000000"
            disabled={locked}
            {...fields('batch_notional')}
          />
          <ConfigurationField
            id="migration-tolerance"
            label="每边金额误差上限"
            unit="%"
            max="50"
            disabled={locked}
            {...fields('tolerance_percent')}
          />
          <details className="disclosure">
            <summary>执行规则</summary>
            <p className="muted">
              只在 SPCX / CL 有有效 5x
              额度、实际杠杆不低于原仓位时迁移，优先选择本批深度价差较小的目标。先开目标多空并确认，再平
              XAU。迁移临时上限为基础加 5 个百分点，最高 100%，计入未平 XAU
              占用并预留四腿成本；保证金不足时等待。常规批次每边至少 500
              USD1，最后尾批可按交易所最小下单规则收尾。
            </p>
          </details>
          <p className="muted">
            {account?.cycle?.enabled
              ? '当前使用独立多空循环，请先完成平仓并退出循环，再开启迁移。'
              : ''}
            开关开启后，普通策略暂停新增仓位，迁移完成后也保持暂停。修改前请暂停账户并等待本批核对完成；保存不会启动账户。关闭再开启会建立新一轮迁移。
          </p>
          <Button
            variant="outline"
            className="full-width"
            disabled={locked}
            type="submit"
          >
            保存迁移设置
          </Button>
        </SettingsForm>
      </details>
    </section>
  );
}
