type OrdinaryAddAccount = {
  policy?: { ordinary_symbol?: string };
  ordinary_add_blocks?: Record<string, string>;
  cycle?: { enabled: boolean; symbol: string };
};

export const ORDINARY_CYCLE_BLOCK_LABEL =
  '本账户成交量循环已启用，普通 5x / 10x / 20x 加仓已禁用';

export function ordinaryAddBlock(
  account: OrdinaryAddAccount | undefined,
  symbol: string,
): string | null {
  const reported = account?.ordinary_add_blocks?.[symbol];
  if (typeof reported === 'string' && reported.trim()) return reported;
  const selected = account?.policy?.ordinary_symbol ?? 'all';
  if (selected !== 'all' && selected !== symbol) {
    return `本账户有额度开仓仅限 ${selected}，禁止 ${symbol} 普通加仓`;
  }
  // Older services already route this account exclusively through cycle mode.
  // Pausing the account does not turn off its saved cycle configuration.
  return account?.cycle?.enabled && account.cycle.symbol === symbol
    ? ORDINARY_CYCLE_BLOCK_LABEL
    : null;
}

export function ordinaryCapacityReady(
  capacity: string | undefined,
  threshold: string,
  live: boolean,
  block: string | null,
): boolean {
  return !block && live && Number(capacity) > Number(threshold);
}
