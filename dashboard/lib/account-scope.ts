type ScopeAccount = {
  policy: { symbols: string[]; ordinary_symbol?: string };
  cycle?: { enabled: boolean; symbol: string };
  migration?: { enabled: boolean };
};

// Describe the saved execution scope, independently of the visible workspace tab.
export function accountRunScope(account: ScopeAccount): string {
  if (account.migration?.enabled) return 'XAU 迁移';
  const ordinary = account.policy.symbols.filter(
    (symbol) =>
      (!account.policy.ordinary_symbol ||
        account.policy.ordinary_symbol === 'all' ||
        account.policy.ordinary_symbol === symbol) &&
      (!account.cycle?.enabled || symbol !== account.cycle.symbol),
  );
  return (
    [
      account.cycle?.enabled ? `循环 ${account.cycle.symbol}` : '',
      ordinary.length ? `普通开仓 ${ordinary.join(' / ')}` : '',
    ]
      .filter(Boolean)
      .join(' · ') || '没有可执行品种'
  );
}
