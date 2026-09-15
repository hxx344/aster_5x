'use client';

/* oxlint-disable jsx-a11y/no-noninteractive-tabindex -- The labeled overflow region needs keyboard focus so arrow keys can scroll the table. */

import { useState } from 'react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import type { CycleReportStatus, CycleTrade } from '@/lib/cycle';
import {
  cycleAmount,
  cycleReportSummary,
  cycleTradeAction,
  cycleTradeCostView,
  cycleTradeDates,
  cycleTradeTimeSource,
  cycleTradesForDate,
  cycleUtcTime,
  validCycleUtcDate,
} from '@/lib/cycle-daily';

type Props = {
  accountName: string;
  trades?: CycleTrade[];
  stale: boolean;
  reportStatus?: CycleReportStatus | null;
  now: number;
};

export function CycleTradesPanel({
  accountName,
  trades,
  stale,
  reportStatus,
  now,
}: Props) {
  const [selectedDate, setSelectedDate] = useState('');
  const dates = cycleTradeDates(trades);
  const rows = cycleTradesForDate(trades, selectedDate);
  const selectedMissing = selectedDate && !dates.includes(selectedDate);
  const report = cycleReportSummary(reportStatus, now);

  return (
    <section
      className="panel cycle-trades-panel"
      aria-labelledby="cycle-trades-heading"
    >
      <div className="section-head cycle-trades-head">
        <div>
          <h2 id="cycle-trades-heading">循环成交明细</h2>
          <p>
            {accountName} · 最近 {trades?.length ?? 0} 条已加载记录
            {stale || report.stale ? ' · 最近记录，等待刷新' : ''}
          </p>
          {report.notice ? (
            <p className={stale || report.stale ? 'amber' : 'muted'}>
              {report.notice}
            </p>
          ) : null}
        </div>
        <div className="cycle-date-filter">
          <label htmlFor="cycle-trade-date">成交日期 · UTC</label>
          <Select
            value={selectedDate || 'all'}
            onValueChange={(value) =>
              value && setSelectedDate(value === 'all' ? '' : value)
            }
          >
            <SelectTrigger id="cycle-trade-date" className="full-width">
              <SelectValue>{selectedDate || '全部已加载日期'}</SelectValue>
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部已加载日期</SelectItem>
              {selectedMissing ? (
                <SelectItem value={selectedDate}>
                  {selectedDate} · 当前未加载
                </SelectItem>
              ) : null}
              {dates.map((date) => (
                <SelectItem key={date} value={date}>
                  {date}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
      </div>
      {rows.length ? (
        <section
          className="cycle-trades-region"
          aria-label="循环成交明细，可横向滚动"
          tabIndex={0}
        >
          <table className="cycle-trade-table">
            <thead>
              <tr>
                <th>成交时间 · UTC</th>
                <th>动作 / 品种</th>
                <th>本笔成交金额 · USD1</th>
                <th>该品种当日累计 · USD1</th>
                <th>本笔已计成本 · USD1</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((trade) => {
                const cost = cycleTradeCostView(trade.cost);
                return (
                  <tr
                    key={`${trade.symbol}:${trade.trade_id}:${trade.order_id}`}
                  >
                    <td>
                      <span className="mobile-trade-label" aria-hidden="true">
                        成交时间 · UTC
                      </span>
                      <time>{cycleUtcTime(trade.executed_at)}</time>
                      <small
                        className={
                          trade.time_source === 'legacy_estimated'
                            ? 'amber'
                            : ''
                        }
                      >
                        {cycleTradeTimeSource(trade)}
                      </small>
                      {!validCycleUtcDate(trade.utc_date) ? (
                        <small className="amber">统计日期缺失</small>
                      ) : null}
                    </td>
                    <td>
                      <span className="mobile-trade-label" aria-hidden="true">
                        动作 / 品种
                      </span>
                      <strong>{trade.symbol || '品种未知'}</strong>
                      <small>{cycleTradeAction(trade)}</small>
                    </td>
                    <td className="cycle-trade-money">
                      <span className="mobile-trade-label" aria-hidden="true">
                        本笔成交金额 · USD1
                      </span>
                      {cycleAmount(trade.notional)}
                    </td>
                    <td className="cycle-trade-money">
                      <span className="mobile-trade-label" aria-hidden="true">
                        该品种当日累计 · USD1
                      </span>
                      {cycleAmount(trade.daily_volume)}
                      {typeof trade.daily_volume !== 'string' ? (
                        <small className="amber">此记录未提供累计值</small>
                      ) : null}
                    </td>
                    <td className="cycle-trade-money cycle-trade-cost">
                      <span className="mobile-trade-label" aria-hidden="true">
                        本笔已计成本 · USD1
                      </span>
                      <strong>{cost.total}</strong>
                      <small>手续费 {cost.fee}</small>
                      <small>本笔计入差价 {cost.spread}</small>
                      {cost.notice ? (
                        <small className="amber">{cost.notice}</small>
                      ) : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </section>
      ) : (
        <div className="cycle-trades-empty">
          {trades === undefined
            ? '成交明细尚未提供；旧版本未记录的成交不会补算为零。'
            : selectedDate
              ? '所选 UTC 日期没有已加载记录。筛选仅覆盖最近 100 条，不代表当日没有其他成交。'
              : '当前账户暂无已记录的循环成交。'}
        </div>
      )}
      <p className="cycle-trades-note">
        最多展示最近 100 条；按 UTC
        日期筛选当前已加载记录。当日累计由交易服务按成交顺序记录，包含开仓、平仓和修复成交，不含手续费。旧记录未提供的金额显示“—”。
        成本中的手续费按成交金额的 0.0125% 计；同批买卖数量按成交先后配对，
        差价只记在较晚成交一笔，负数抵减成本。未配对成交只先计手续费，成本尚未完整。
      </p>
    </section>
  );
}
