import { percentFromMarginLimit } from '@/lib/policy';
import { formatNumber } from '@/lib/number-format';
import type { ordinaryConditionsView } from '@/lib/ordinary-conditions';

type View = ReturnType<typeof ordinaryConditionsView>;
type Condition = View['spread'];

function amount(
  value: string | null,
  unit: 'USD1' | '%' | 'bp',
  compact = true,
) {
  if (value === null) return '—';
  let shifted: string;
  try {
    shifted =
      unit === '%'
        ? percentFromMarginLimit(value)
        : unit === 'bp'
          ? percentFromMarginLimit(percentFromMarginLimit(value))
          : value;
  } catch {
    return `${value} × ${unit === '%' ? '100' : '10000'}`;
  }
  if (/[eE]/.test(shifted)) return shifted;
  const [whole, fraction] = shifted.split('.');
  if (compact && fraction && /[1-9]/.test(fraction.slice(4)))
    return `≈${formatNumber(shifted, 4)}`;
  return `${whole.replace(/\B(?=(\d{3})+(?!\d))/g, ',')}${fraction === undefined ? '' : `.${fraction}`}`;
}

function Check({
  label,
  value,
  unit,
  compact = true,
}: {
  label: string;
  value: Condition;
  unit: 'USD1' | '%' | 'bp';
  compact?: boolean;
}) {
  return (
    <div className="ordinary-condition">
      <div className="ordinary-condition-heading">
        <span>{label}</span>
        <strong className={value.tone}>{value.status}</strong>
      </div>
      <div className="ordinary-condition-value">
        <span>
          {amount(value.actual, unit, compact)} {unit}
        </span>
        <span>
          要求 {value.operator} {amount(value.required, unit, compact)} {unit}
        </span>
      </div>
    </div>
  );
}

export function OrdinaryConditions({
  view,
  cycleSymbolActive,
}: {
  view: View;
  cycleSymbolActive: boolean;
}) {
  return (
    <div className="ordinary-conditions compact-conditions">
      <p className="ordinary-conditions-context">
        当前品种持仓杠杆：
        {view.currentLeverage === null
          ? '未核验'
          : `${view.currentLeverage}x${cycleSymbolActive ? '（循环品种）' : ''}`}
        ； 普通最低开仓杠杆：
        {view.minimumLeverage === null ? '未核验' : `${view.minimumLeverage}x`}
        。
      </p>
      {!cycleSymbolActive && view.leverageConstraint ? (
        <p className="ordinary-conditions-context amber">
          {view.leverageConstraint === 'unsupported'
            ? `当前 ${view.currentLeverage}x 不支持普通新增开仓。`
            : `当前 ${view.currentLeverage}x，需先升至至少 ${view.minimumLeverage}x 才能普通新增开仓。`}
        </p>
      ) : null}
      <Check label="BBO 价差" value={view.spread} unit="bp" />
      {view.tiers.map((tier) => (
        <section
          key={tier.leverage}
          className="ordinary-tier"
          aria-label={`普通 ${tier.leverage}x 条件`}
        >
          <div className="ordinary-tier-heading">
            <h3>{tier.leverage}x</h3>
            {tier.belowMinimum ? (
              <span className="amber">低于最低开仓杠杆</span>
            ) : tier.current ? (
              <span className="muted">当前持仓档位</span>
            ) : null}
          </div>
          <Check label="公开额度" value={tier.capacity} unit="USD1" />
          <Check label="当前保证金占用率" value={tier.margin} unit="%" />
        </section>
      ))}
      <details className="disclosure ordinary-precision">
        <summary>精确数值与条件口径</summary>
        <Check label="BBO 价差" value={view.spread} unit="bp" compact={false} />
        {view.tiers.map((tier) => (
          <section key={tier.leverage} className="ordinary-tier">
            <h3>{tier.leverage}x</h3>
            <Check
              label="公开额度"
              value={tier.capacity}
              unit="USD1"
              compact={false}
            />
            <Check
              label="当前保证金占用率"
              value={tier.margin}
              unit="%"
              compact={false}
            />
          </section>
        ))}
        <p className="ordinary-conditions-context">
          ≈
          为显示近似值，条件判断使用精确数值。保证金条件按当前占用率比较，未估算调整杠杆后的占用；余额、最小批次和持仓平衡仍需在下单前核验。
        </p>
      </details>
    </div>
  );
}
