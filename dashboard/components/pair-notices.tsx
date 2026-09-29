'use client';
import { Button } from '@/components/ui/button';
import { clock } from '@/lib/desk-format';
import type { Pair } from '@/lib/pairs';
import {
  pairScheduledMarginWait,
  pairStatusNotices,
  recordPairNotices,
  type PairNoticeEntry,
  type PairNoticeHistory,
} from '@/lib/pair-notices';

export function PairNotices({
  pair,
  now,
  offline,
  history,
  clear,
}: {
  pair: Pair;
  now: number;
  offline: boolean;
  history?: PairNoticeHistory;
  clear: () => void;
}) {
  const notices = pairStatusNotices(pair, now, offline);
  const scheduledWait = pairScheduledMarginWait(pair, now, offline);
  const currentKeys = new Set(notices.map((notice) => notice.key));
  // Derive current warnings immediately; history never authorizes an action.
  const entries = recordPairNotices(history, notices, now).entries;
  const latest = entries[0];
  const visible = notices.map(
    (notice) =>
      entries.find((entry) => entry.key === notice.key) ?? {
        ...notice,
        firstSeen: now,
        lastSeen: now,
        occurrences: 1,
      },
  );
  if (latest && !currentKeys.has(latest.key)) visible.push(latest);
  const visibleKeys = new Set(visible.map((entry) => entry.key));
  const older = entries.filter((entry) => !visibleKeys.has(entry.key));
  if (!visible.length && !scheduledWait) return null;
  const hasPast = entries.some((entry) => !currentKeys.has(entry.key));
  const status = (entry: PairNoticeEntry) => {
    if (currentKeys.has(entry.key)) return '当前提示';
    if (
      entry.kind === 'data' &&
      !scheduledWait &&
      Number.isFinite(now) &&
      now > 0 &&
      !notices.some((notice) => notice.kind === 'data')
    )
      return '历史提示 · 数据已恢复';
    return '历史提示 · 当前已不再显示';
  };
  const item = (entry: PairNoticeEntry, announce = false) => (
    <div key={entry.key} className="pair-notice-item">
      <p
        className={currentKeys.has(entry.key) ? 'amber' : 'muted'}
        aria-live={announce ? 'polite' : undefined}
      >
        <strong>{status(entry)}</strong> ·{' '}
        {entry.source === 'pair'
          ? '配对执行 API · '
          : entry.source === 'margin'
            ? '保证金管理 API · '
            : ''}
        {entry.text}
      </p>
      <p className="pair-notice-time muted">
        首次看到 {clock(entry.firstSeen)} · 最近看到 {clock(entry.lastSeen)}
        {entry.occurrences > 1 ? ` · 共出现 ${entry.occurrences} 次` : ''}
      </p>
    </div>
  );
  return (
    <section className="pair-notices" aria-label="配对组最近提示">
      <div className="pair-notices-heading">
        <span>最近提示</span>
        {hasPast ? (
          <Button variant="ghost" size="sm" onClick={clear}>
            清除历史提示
          </Button>
        ) : null}
      </div>
      {scheduledWait ? (
        <p className="muted" aria-live="polite">
          {scheduledWait.checking
            ? '等待本次保证金检查结果'
            : scheduledWait.coolingDown
              ? '划转冷却中'
              : '等待下一次保证金检查'}{' '}
          · 上次检查 {clock(scheduledWait.checkedAt)} ·{' '}
          {scheduledWait.checking ? '计划检查' : '下一次检查'}{' '}
          {clock(scheduledWait.nextCheckAt)}
          。账户数值保留上次快照，实际划转前会重新核验。
        </p>
      ) : null}
      {visible.map((entry) => item(entry, true))}
      {older.length ? (
        <details className="pair-notices-history">
          <summary>查看其他 {older.length} 条提示</summary>
          {older.map((entry) => item(entry))}
        </details>
      ) : null}
      {entries.length ? (
        <p className="muted pair-notice-scope">
          仅保留本次页面会话最近 8
          种提示，按配对组分别记录；历史提示不代表当前仍有异常。
        </p>
      ) : null}
    </section>
  );
}
