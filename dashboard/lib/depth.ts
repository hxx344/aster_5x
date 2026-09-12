export const DEPTH_NOTIONALS = [10000, 50000] as const;
const MAX_AGE = 15;

export type DepthQuote = {
  timestamp: number;
  levels_limit: number;
  spreads: Record<
    string,
    {
      status: 'ok' | 'insufficient';
      buy_average: string | null;
      sell_average: string | null;
      spread: string | null;
    }
  >;
};

export function depthQuoteView(
  depth: DepthQuote | undefined,
  notional: number,
  now: number,
  error?: string,
) {
  const row = depth?.spreads[String(notional)];
  const time = depth
    ? new Date(depth.timestamp * 1000).toLocaleTimeString('zh-CN', {
        hour12: false,
      })
    : '';
  const age = depth ? now - depth.timestamp : Infinity;
  const stale =
    Boolean(error) || !Number.isFinite(age) || age < -1 || age > MAX_AGE;
  if (!row) {
    return {
      value: error ? '获取失败' : '等待深度',
      detail: error || '等待首次采样',
      title: error || '等待深度报价',
      stale: true,
    };
  }
  const insufficient = row.status === 'insufficient';
  const spread = row.spread === null ? NaN : Number(row.spread);
  const valid = row.status === 'ok' && Number.isFinite(spread) && spread >= 0;
  const missing = [
    row.buy_average === null ? '买入深度不足' : '',
    row.sell_average === null ? '卖出深度不足' : '',
  ]
    .filter(Boolean)
    .join('、');
  return {
    value: insufficient
      ? '深度不足'
      : valid
        ? `${(spread * 10000).toLocaleString('en-US', {
            minimumFractionDigits: 2,
            maximumFractionDigits: 2,
          })} bp`
        : '数据无效',
    detail: error
      ? `更新失败 · ${time}`
      : stale
        ? `已过期 · ${time}`
        : missing || `更新 ${time}`,
    title: [
      error,
      `采样时间 ${time}；买卖盘各最多 ${depth?.levels_limit} 档`,
      missing || `买入均价 ${row.buy_average}；卖出均价 ${row.sell_average}`,
      '每边分别成交指定 USD1 金额；价差 = (买入均价 − 卖出均价) / 两者中间价；不含手续费',
    ]
      .filter(Boolean)
      .join('\n'),
    stale: stale || (!valid && !insufficient),
  };
}
