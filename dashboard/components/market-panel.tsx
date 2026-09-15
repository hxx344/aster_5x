'use client';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import type { Account, Market } from '@/lib/desk-types';
import { fmt, clock, names, symbols } from '@/lib/desk-format';
import { SUPPORTED_LEVERAGES } from '@/lib/policy';
import { ordinaryAddBlock, ordinaryCapacityReady } from '@/lib/ordinary-add';
import { accountCapacityView } from '@/lib/account-capacity';
import { DEPTH_NOTIONALS, depthQuoteView } from '@/lib/depth';
export function MarketPanel({
  account,
  markets,
  now,
  connectionError,
  focus,
  setFocus,
}: {
  account: Account;
  markets: Record<string, Market>;
  now: number;
  connectionError: string;
  focus: string;
  setFocus: (symbol: string) => void;
}) {
  const snapshot = account.snapshot;
  return (
    <section className="panel">
      <div className="section-head">
        <div>
          <h2>市场额度</h2>
          <p>市场额度与账户当前杠杆余量 · USD1</p>
        </div>
        <span className="small-note">
          额度 &gt; {fmt(account?.policy.threshold || 10000, 0)} 才触发
        </span>
      </div>
      <section
        className="table-scroll"
        aria-label="市场额度与深度价差，可横向滚动"
      >
        <Table className="market-table">
          <TableHeader>
            <TableRow>
              <TableHead>市场</TableHead>
              {SUPPORTED_LEVERAGES.map((v) => (
                <TableHead key={v} className="number">
                  {v}x
                </TableHead>
              ))}
              <TableHead className="number">当前杠杆 / 账户余量</TableHead>
              {DEPTH_NOTIONALS.map((amount) => (
                <TableHead key={amount} className="number">
                  {amount / 10000} 万 USD1
                  <span className="depth-heading">深度价差</span>
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody>
            {symbols.map((s) => {
              const m = markets[s];
              const live =
                !connectionError &&
                m?.status === 'ok' &&
                now - m.checked_at >= -1 &&
                now - m.checked_at <= 8;
              const lev = snapshot?.positions.find(
                (p) => p.symbol === s,
              )?.leverage;
              const ordinaryBlock = ordinaryAddBlock(account, s);
              const room = snapshot?.account_capacity?.[s];
              const roomView = accountCapacityView(
                room,
                lev,
                now,
                !!connectionError,
              );
              return (
                <TableRow key={s} className={focus === s ? 'selected-row' : ''}>
                  <TableCell>
                    <button className="market-name" onClick={() => setFocus(s)}>
                      <strong>{s}</strong>
                      <span>
                        {names[s]} {!live && '· 等待数据'}
                      </span>
                    </button>
                    {ordinaryBlock ? (
                      <p className="ordinary-add-block" title={ordinaryBlock}>
                        {ordinaryBlock}
                      </p>
                    ) : null}
                  </TableCell>
                  {SUPPORTED_LEVERAGES.map((v) => {
                    const stamp =
                      m?.capacity_checked_at?.[v] ?? m?.checked_at ?? 0;
                    const tierLive =
                      live && now - stamp >= -1 && now - stamp <= 8;
                    return (
                      <TableCell
                        key={v}
                        className={`number ${ordinaryCapacityReady(m?.capacities[v], account?.policy.threshold || '10000', tierLive, ordinaryBlock) ? 'mint' : ''}`}
                      >
                        <span className="mobile-cell-label">{v}x 额度</span>
                        {tierLive ? fmt(m?.capacities[v], 0) : '—'}
                      </TableCell>
                    );
                  })}
                  <TableCell className="number account-capacity-cell">
                    <span className="mobile-cell-label">当前杠杆</span>
                    {lev ? <span className="leverage-chip">{lev}x</span> : '—'}
                    <div
                      className="account-capacity"
                      title={
                        roomView.ready && room
                          ? `${room.leverage}x 账户上限 ${fmt(room.cap, 0)} − 多空合计持仓 ${fmt(room.occupied, 0)}；同步于 ${clock(room.checked_at)}`
                          : roomView.detail
                      }
                    >
                      <span>
                        {roomView.ready ? fmt(room?.remaining, 0) : '—'}
                      </span>
                      <small>{roomView.detail}</small>
                    </div>
                  </TableCell>
                  {DEPTH_NOTIONALS.map((amount) => {
                    const quote = depthQuoteView(
                      m?.depth,
                      amount,
                      now,
                      connectionError || m?.depth_error,
                    );
                    return (
                      <TableCell key={amount} className="number">
                        <span className="mobile-cell-label">
                          {amount / 10000} 万 USD1 深度价差
                        </span>
                        <div
                          className={`depth-quote ${quote.stale ? 'depth-stale' : ''}`}
                          title={quote.title}
                        >
                          <span>{quote.value}</span>
                          <small>{quote.detail}</small>
                        </div>
                      </TableCell>
                    );
                  })}
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </section>
      <div className="table-footer">
        <span>
          <i className="dot mint-bg" />
          按名义金额比较，多空不抵消
        </span>
        <span>1 bp = 万分之一</span>
      </div>
      <details className="disclosure inset">
        <summary>数据口径与采样时间</summary>
        <p className="depth-method">
          账户余量 = 当前杠杆账户上限 −
          多空合计持仓；可开金额还受市场额度、余额与保证金限制。
          <br />
          深度价差按买入、卖出每边各 1 万 / 5 万 USD1
          的成交均价计算，不含手续费。 每 10 秒采样，超过 15
          秒标记过期；买卖盘各最多 1,000 档，1 bp = 万分之一。
        </p>
      </details>
    </section>
  );
}
