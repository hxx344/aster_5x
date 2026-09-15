'use client';
import { useState } from 'react';

// Each feature owns its drafts. A refresh or account switch must not overwrite edits.
export function useAccountDraft<T>(accountId: string, saved: T) {
  const [drafts, setDrafts] = useState<Record<string, T>>({});
  const value = Object.hasOwn(drafts, accountId) ? drafts[accountId] : saved;
  const setValue = (next: T) =>
    setDrafts((previous) => ({ ...previous, [accountId]: next }));
  const clear = () =>
    setDrafts((previous) => {
      const next = { ...previous };
      delete next[accountId];
      return next;
    });
  return {
    value,
    setValue,
    clear,
    dirty: JSON.stringify(value) !== JSON.stringify(saved),
  };
}
