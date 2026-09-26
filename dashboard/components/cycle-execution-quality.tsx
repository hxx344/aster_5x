'use client';
import { useId } from 'react';
import {
  cycleExecutionQualityView,
  type CycleExecutionQuality,
} from '@/lib/cycle-quality';

export function CycleExecutionQualityPanel({
  quality,
  accountName,
  stale,
  title = '最近循环成交质量',
}: {
  quality?: CycleExecutionQuality | null;
  accountName: string;
  stale: boolean;
  title?: string;
}) {
  const headingId = useId();
  const view = cycleExecutionQualityView(quality);
  return (
    <section className="cycle-execution-quality" aria-labelledby={headingId}>
      <div className="cycle-quality-heading">
        <h3 id={headingId}>{title}</h3>
        <span>{accountName}</span>
      </div>
      {view ? (
        <>
          <p className="cycle-quality-batch">
            <strong>
              {view.symbol} · {view.phase}
            </strong>
            <span>本批每边数量 {view.quantity}</span>
          </p>
          <p className="cycle-quality-recorded">
            最后记录 <time>{view.updatedAt}</time>
          </p>
          {stale ? (
            <p className="cycle-quality-notice amber">
              状态同步中断，以下为最近记录。
            </p>
          ) : null}
          <table className="cycle-quality-comparison">
            <caption>同一批每边数量比较 · 价差单位 bp · 历史记录</caption>
            <thead>
              <tr>
                <th scope="col">阶段</th>
                <th scope="col">双边价差 · bp</th>
                <th scope="col">状态</th>
              </tr>
            </thead>
            <tbody>
              {[
                { key: 'trigger', label: '触发时预计', ...view.trigger },
                { key: 'final', label: '发单前预计', ...view.final },
                {
                  key: 'actual',
                  label: '实际双边成交',
                  ...view.actual,
                  available: view.actual.complete,
                },
              ].map((row) => (
                <tr key={row.key}>
                  <th scope="row">{row.label}</th>
                  <td className="cycle-quality-value">
                    <span
                      className="cycle-quality-mobile-unit"
                      aria-hidden="true"
                    >
                      价差 · bp
                    </span>
                    <strong>{row.spread}</strong>
                  </td>
                  <td className={row.available ? '' : 'amber'}>{row.status}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="cycle-quality-note">
            价差 =（买入均价 − 卖出均价）÷ 双边均价中值 ×
            10,000；负值表示有利价差。未确认或数量不一致时不作成交价差比较。
          </p>
          {view.actual.repairs ? (
            <p className="cycle-quality-notice amber">
              仅比较原始双边订单；本批发生过修复，修复成交未合并。
            </p>
          ) : null}
          <dl className="cycle-quality-timings">
            {view.timing.map((item) => (
              <div key={item.label}>
                <dt>{item.label}</dt>
                <dd>
                  {item.value} <span>ms</span>
                </dd>
              </div>
            ))}
          </dl>
          <p className="cycle-quality-note">
            请求开始为本地调用边界；耗时包含本地处理与网络往返，不代表交易所撮合延迟。
          </p>
          <details className="cycle-quality-details">
            <summary>盘口、成交与响应明细</summary>
            <div className="cycle-quality-detail-body">
              <dl className="cycle-quality-fields">
                <div>
                  <dt>批次</dt>
                  <dd>{view.intentId}</dd>
                </div>
                <div>
                  <dt>创建记录</dt>
                  <dd>{view.createdAt}</dd>
                </div>
                <div>
                  <dt>触发来源</dt>
                  <dd>{view.triggerSource}</dd>
                </div>
                <div>
                  <dt>触发接收时间</dt>
                  <dd>{view.triggerReceivedAt}</dd>
                </div>
              </dl>
              <section className="cycle-quality-stage">
                <h4>发单准备耗时</h4>
                <p className="cycle-quality-note">
                  各项均发生在请求开始前；旧批次可能未记录。
                </p>
                <dl className="cycle-quality-fields">
                  {view.preSubmit.map((item) => (
                    <div key={item.key}>
                      <dt>{item.label}</dt>
                      <dd>{item.value} ms</dd>
                    </div>
                  ))}
                </dl>
              </section>
              <section className="cycle-quality-stage">
                <h4>数据库与提交调用分解</h4>
                <p className="cycle-quality-note">
                  数据库耗时从执行器开始计至提交调用，已包含在上方准备耗时中。HTTP
                  调用包含连接处理和网络往返；失败调用也记录耗时，未记录的阶段显示“—”。
                </p>
                <dl className="cycle-quality-fields">
                  {[
                    ...view.database,
                    { label: '提交调用 → HTTP 开始', value: view.beforeHttp },
                    ...view.transport,
                  ].map((item) => (
                    <div key={item.label}>
                      <dt>{item.label}</dt>
                      <dd>{item.value} ms</dd>
                    </div>
                  ))}
                </dl>
              </section>
              {[
                {
                  key: 'trigger',
                  title: '触发时预计',
                  ...view.trigger,
                },
                { key: 'final', title: '发单前预计', ...view.final },
              ].map((stage) => (
                <section className="cycle-quality-stage" key={stage.key}>
                  <h4>{stage.title}</h4>
                  <dl className="cycle-quality-fields">
                    <div>
                      <dt>每边估计数量</dt>
                      <dd>{stage.quantity}</dd>
                    </div>
                    <div>
                      <dt>盘口记录时间</dt>
                      <dd>{stage.sampledAt}</dd>
                    </div>
                    <div>
                      <dt>深度检查时间</dt>
                      <dd>{stage.checkedAt}</dd>
                    </div>
                    <div>
                      <dt>买入深度均价 · USD1</dt>
                      <dd>{stage.buy}</dd>
                    </div>
                    <div>
                      <dt>卖出深度均价 · USD1</dt>
                      <dd>{stage.sell}</dd>
                    </div>
                  </dl>
                </section>
              ))}
              <section className="cycle-quality-stage">
                <h4>原始双边成交</h4>
                <dl className="cycle-quality-fields">
                  <div>
                    <dt>已确认每边数量</dt>
                    <dd>{view.actual.quantity}</dd>
                  </div>
                  <div>
                    <dt>买入已成交数量</dt>
                    <dd>{view.actual.buyQuantity}</dd>
                  </div>
                  <div>
                    <dt>卖出已成交数量</dt>
                    <dd>{view.actual.sellQuantity}</dd>
                  </div>
                  <div>
                    <dt>买入成交均价 · USD1</dt>
                    <dd>{view.actual.buy}</dd>
                  </div>
                  <div>
                    <dt>卖出成交均价 · USD1</dt>
                    <dd>{view.actual.sell}</dd>
                  </div>
                  <div>
                    <dt>买入订单状态</dt>
                    <dd>{view.actual.buyStatus}</dd>
                  </div>
                  <div>
                    <dt>卖出订单状态</dt>
                    <dd>{view.actual.sellStatus}</dd>
                  </div>
                </dl>
              </section>
              <section className="cycle-quality-stage">
                <h4>请求与响应</h4>
                <dl className="cycle-quality-fields">
                  <div>
                    <dt>请求状态</dt>
                    <dd>{view.requestStatus}</dd>
                  </div>
                  <div>
                    <dt>请求开始时间</dt>
                    <dd>{view.requestStartedAt}</dd>
                  </div>
                  <div>
                    <dt>收到响应时间</dt>
                    <dd>{view.responseReceivedAt}</dd>
                  </div>
                </dl>
              </section>
            </div>
          </details>
        </>
      ) : (
        <p className="cycle-quality-note">暂无可用的循环成交质量记录。</p>
      )}
    </section>
  );
}
