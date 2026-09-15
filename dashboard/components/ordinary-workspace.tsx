'use client';
import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import type { FeatureProps } from '@/lib/desk-types';
import { useAccountDraft } from '@/lib/use-account-draft';
import { SlidersHorizontal } from 'lucide-react';
import type { Market, PolicyDraft } from '@/lib/desk-types';
import { names, symbols } from '@/lib/desk-format';
import { SUPPORTED_LEVERAGES, parseMinimumLeverage } from '@/lib/policy';
import { ordinaryConditionsView } from '@/lib/ordinary-conditions';
import { ordinaryAddBlock } from '@/lib/ordinary-add';
import { accountConfigurationLock } from '@/lib/account-config';
import { OrdinaryConditions } from '@/components/ordinary-conditions';
import { MarketPanel } from '@/components/market-panel';
export function OrdinaryWorkspace({
  account,
  busy,
  action,
  setError,
  setNotice,
  markets,
  now,
  connectionError,
}: FeatureProps & {
  markets: Record<string, Market>;
  now: number;
  connectionError: string;
}) {
  const [focus, setFocus] = useState('XAUUSD1');
  const configurationLock = accountConfigurationLock(account);
  const locked = busy || Boolean(configurationLock);
  const minimumLeverage = account.policy.min_open_leverage ?? 5;
  const {
    value: form,
    setValue: setForm,
    clear: clearDraft,
  } = useAccountDraft<PolicyDraft>(account.id, {
    threshold: account.policy.threshold,
    order_notional: account.policy.order_notional,
    min_open_leverage: SUPPORTED_LEVERAGES.includes(minimumLeverage)
      ? String(minimumLeverage)
      : '',
    ordinary_symbol: account.policy.ordinary_symbol ?? 'all',
  });
  const strategy = account.strategies[focus];
  const focusedOrdinaryBlock = ordinaryAddBlock(account, focus);
  const ordinaryConditions = ordinaryConditionsView(
    account,
    markets[focus],
    focus,
    now,
    connectionError,
  );
  return (
    <div className="feature-stack">
      <div className="feature-grid">
        {' '}
        <section className="panel execution-panel" aria-label="普通加仓条件">
          <div className="section-head">
            <h2>普通加仓条件</h2>
            <Select
              value={focus}
              onValueChange={(value) => value && setFocus(value)}
            >
              <SelectTrigger aria-label="查看普通开仓品种">
                <SelectValue>{focus}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                {symbols.map((symbol) => (
                  <SelectItem key={symbol} value={symbol}>
                    {names[symbol]} · {symbol}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <OrdinaryConditions
            view={ordinaryConditions}
            cycleSymbolActive={Boolean(
              account?.cycle?.enabled && account.cycle.symbol === focus,
            )}
          />
          <div className="strategy-state">
            <i
              className={`dot ${account?.enabled && !focusedOrdinaryBlock ? 'mint-bg' : 'amber-bg'}`}
            />
            <span>
              {focusedOrdinaryBlock ||
                strategy?.reason ||
                account?.reason ||
                '等待接入账户'}
            </span>
          </div>
        </section>
        <section className="panel settings-panel">
          <div className="section-head">
            <h2>普通开仓设置</h2>
            <SlidersHorizontal size={17} />
          </div>
          <dl className="saved-settings">
            <div>
              <dt>开仓品种</dt>
              <dd>
                {account.policy.ordinary_symbol &&
                account.policy.ordinary_symbol !== 'all'
                  ? account.policy.ordinary_symbol
                  : '全部品种'}
              </dd>
            </div>
            <div>
              <dt>最低开仓杠杆</dt>
              <dd>{minimumLeverage}x</dd>
            </div>
            <div>
              <dt>额度阈值 · USD1</dt>
              <dd>{account.policy.threshold}</dd>
            </div>
            <div>
              <dt>单笔每边上限 · USD1</dt>
              <dd>{account.policy.order_notional}</dd>
            </div>
          </dl>
          {configurationLock ? (
            <p className="settings-lock muted">{configurationLock}</p>
          ) : null}
          <details className="disclosure inset">
            <summary>编辑普通开仓参数</summary>
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                if (locked) return;
                try {
                  const policy = {
                    ordinary_symbol: form.ordinary_symbol,
                    threshold: form.threshold,
                    order_notional: form.order_notional,
                    min_open_leverage: parseMinimumLeverage(
                      form.min_open_leverage,
                    ),
                  };
                  if (
                    await action(`/api/accounts/${account.id}`, policy, 'PATCH')
                  ) {
                    clearDraft();
                    setNotice('策略设置已保存');
                  }
                } catch (e) {
                  setNotice('');
                  setError(e instanceof Error ? e.message : '设置无效');
                }
              }}
            >
              <label htmlFor="ordinary-symbol">
                有额度开仓交易对
                <Select
                  required
                  disabled={locked}
                  value={form.ordinary_symbol}
                  onValueChange={(value) =>
                    value && setForm({ ...form, ordinary_symbol: value })
                  }
                >
                  <SelectTrigger id="ordinary-symbol" className="full-width">
                    <SelectValue>
                      {form.ordinary_symbol === 'all'
                        ? '全部交易对'
                        : `仅 ${form.ordinary_symbol}`}
                    </SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="all">全部交易对</SelectItem>
                    {(account?.policy.symbols ?? symbols).map((symbol) => (
                      <SelectItem key={symbol} value={symbol}>
                        仅 {symbol}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <span>
                  指定后，普通开仓与升杠杆仅操作所选交易对。循环与迁移按各自设置执行。
                </span>
              </label>
              <label htmlFor="threshold">
                额度阈值 <span>USD1</span>
                <Input
                  id="threshold"
                  type="number"
                  min="0"
                  step="any"
                  required
                  disabled={locked}
                  value={form.threshold}
                  onChange={(e) =>
                    setForm({ ...form, threshold: e.target.value })
                  }
                />
              </label>
              <label htmlFor="order-notional">
                单笔每边上限 <span>USD1</span>
                <Input
                  id="order-notional"
                  type="number"
                  min="500"
                  max="1000000"
                  step="any"
                  required
                  disabled={locked}
                  value={form.order_notional}
                  onChange={(e) =>
                    setForm({ ...form, order_notional: e.target.value })
                  }
                />
                <span>新开仓固定每边至少 500 USD1</span>
              </label>
              <label htmlFor="min-open-leverage">
                最低开仓杠杆 <span>x</span>
                <Select
                  required
                  disabled={locked}
                  value={form.min_open_leverage || null}
                  onValueChange={(value) =>
                    value && setForm({ ...form, min_open_leverage: value })
                  }
                >
                  <SelectTrigger id="min-open-leverage" className="full-width">
                    <SelectValue placeholder="请选择最低开仓杠杆" />
                  </SelectTrigger>
                  <SelectContent>
                    {SUPPORTED_LEVERAGES.map((value) => (
                      <SelectItem key={value} value={String(value)}>
                        {value}x
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </label>
              <p className="muted">
                暂停账户后可修改。基础风险上限在「账户」页统一设置；实际开仓仍需通过余额、价差和持仓检查。
              </p>
              <Button
                variant="outline"
                className="full-width"
                disabled={locked}
                type="submit"
              >
                保存设置
              </Button>
            </form>
          </details>
        </section>
      </div>
      <details className="disclosure panel">
        <summary>全部市场额度与深度</summary>
        <MarketPanel
          account={account}
          markets={markets}
          now={now}
          connectionError={connectionError}
          focus={focus}
          setFocus={setFocus}
        />
      </details>
    </div>
  );
}
