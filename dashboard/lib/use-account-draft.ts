'use client';
import { useState } from 'react';

type TextKey<T> = { [K in keyof T]: T[K] extends string ? K : never }[keyof T];

export function draftFields<T>(draft: T, setDraft: (next: T) => void) {
  return (key: TextKey<T>) => ({
    value: draft[key] as string,
    onValueChange: (value: string) => setDraft({ ...draft, [key]: value }),
  });
}

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
