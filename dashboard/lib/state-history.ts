type HistoryAccount<T> = {
  id: string;
  cycle_trades?: T[];
  cycle_trades_revision?: string;
};

export function createStateHistory<T>() {
  const entries = new Map<string, { revision: string; trades: T[] }>();
  let selected = '';
  return {
    clear() {
      entries.clear();
      selected = '';
    },
    select(accountId: string) {
      selected = accountId;
    },
    selected() {
      return selected;
    },
    url(accountId = selected) {
      const params = new URLSearchParams({ compact: 'true' });
      if (accountId) {
        params.set('history_account', accountId);
        params.set('history_revision', entries.get(accountId)?.revision ?? '');
      }
      return `/api/state?${params}`;
    },
    merge<A extends HistoryAccount<T>>(accounts: A[]): A[] {
      const live = new Set(accounts.map((account) => account.id));
      if (!live.has(selected)) selected = accounts[0]?.id ?? '';
      for (const id of entries.keys()) if (!live.has(id)) entries.delete(id);
      return accounts.map((account) => {
        const revision = account.cycle_trades_revision;
        if (!revision) return account;
        if (account.cycle_trades !== undefined) {
          entries.set(account.id, { revision, trades: account.cycle_trades });
          return account;
        }
        const cached = entries.get(account.id);
        // Only the server's current revision can authorize a reused list.
        if (cached?.revision === revision)
          return { ...account, cycle_trades: cached.trades };
        return account;
      });
    },
  };
}
