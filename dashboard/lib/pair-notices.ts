import {
  pairDataFresh,
  pairHasPending,
  pairPhaseLabel,
  pairStartRecoveryBlock,
  pairTransferStatus,
  type PairApiNotice,
  type Pair,
} from './pairs.ts';

export type PairNotice = {
  key: string;
  kind: 'data' | 'execution' | 'api';
  text: string;
  source?: 'pair' | 'margin';
};
export type PairNoticeEntry = PairNotice & {
  firstSeen: number;
  lastSeen: number;
  occurrences: number;
};
export type PairNoticeHistory = {
  activeKeys: string[];
  entries: PairNoticeEntry[];
};
export const PAIR_NOTICE_LIMIT = 8;

function apiNotice(
  current: PairApiNotice | null | undefined,
  legacyReason: unknown,
): PairApiNotice | null {
  if (current !== undefined) {
    return current &&
      ['budget', 'cooldown', 'rate_limit'].includes(current.kind) &&
      typeof current.text === 'string' &&
      current.text.trim()
      ? current
      : null;
  }
  // Only old servers lacking the field use this narrow compatibility path.
  // Explicit null means the server cleared the API notice, even if its general
  // display reason has not changed yet. Trading capacity is not API quota.
  if (
    typeof legacyReason === 'string' &&
    /本地(?:普通|执行)?\s*API\s*请求权重预算不足/.test(legacyReason)
  )
    return { kind: 'budget', text: legacyReason };
  return null;
}

export function pairScheduledMarginWait(
  pair: Pair,
  now: number,
  offline = false,
): {
  checkedAt: number;
  nextCheckAt: number;
  coolingDown: boolean;
  checking: boolean;
} | null {
  const state = pair.state;
  const margin = state?.margin;
  // Display-only: the backend intentionally retains snapshots between balance
  // checks. This must never relax freshness checks used by trading controls.
  if (
    !pair.enabled ||
    pair.ordinary?.enabled !== false ||
    pair.cycle?.enabled !== false ||
    pair.margin?.enabled !== true ||
    margin?.enabled !== true ||
    margin.blocks_trading !== false ||
    state?.phase !== 'monitoring' ||
    !['waiting', 'cooldown', 'confirmed'].includes(margin.status ?? '') ||
    !pairDataFresh(state.updated_at, now, offline) ||
    pairHasPending(pair) ||
    Object.values(state.progress?.quantities ?? {}).some(
      (quantity) =>
        typeof quantity !== 'string' || !/^0(?:\.0+)?$/.test(quantity),
    ) ||
    apiNotice(state.api_notice, state.reason) ||
    apiNotice(margin.api_notice, margin.reason)
  )
    return null;
  const checkedAt = margin.checked_at;
  const nextCheckAt = margin.next_check_at;
  const cooldownUntil = margin.cooldown_until ?? 0;
  const validTime = (stamp: unknown): stamp is number =>
    typeof stamp === 'number' && Number.isFinite(stamp) && stamp > 0;
  if (
    !validTime(checkedAt) ||
    checkedAt > now + 1 ||
    !validTime(nextCheckAt) ||
    nextCheckAt <= checkedAt ||
    !Number.isFinite(cooldownUntil) ||
    cooldownUntil < 0 ||
    !['long', 'short'].every((side) => {
      const snapshot = state.snapshots?.[side as 'long' | 'short'];
      return validTime(snapshot?.timestamp) && snapshot.timestamp <= now + 1;
    })
  )
    return null;
  const deadline = Math.max(nextCheckAt, cooldownUntil);
  // A scheduled read takes time. Give its still-live worker the same bounded
  // eight-second window; do not flash a warning at every check boundary.
  return now - deadline < 8
    ? {
        checkedAt,
        nextCheckAt: deadline,
        coolingDown: cooldownUntil > now,
        checking: now >= deadline,
      }
    : null;
}

export function pairStatusNotices(
  pair: Pair,
  now: number,
  offline = false,
): PairNotice[] {
  const notices: PairNotice[] = [];
  const add = (kind: PairNotice['kind'], text: string) =>
    notices.push({ key: `${kind}:${text}`, kind, text });
  const pairApiNotice = apiNotice(pair.state?.api_notice, pair.state?.reason);
  const marginApiNotice = apiNotice(
    pair.state?.margin?.api_notice,
    pair.state?.margin?.reason,
  );
  const snapshots = pair.state?.snapshots;
  const canCheckAtStart =
    !pair.enabled && pairHasPending(pair) && !pairStartRecoveryBlock(pair);
  const canResumeTransfer =
    canCheckAtStart && pair.state?.margin?.trading_resume_allowed === true;
  const fresh =
    pairDataFresh(pair.state?.updated_at, now, offline) &&
    pairDataFresh(snapshots?.long?.timestamp, now, offline) &&
    pairDataFresh(snapshots?.short?.timestamp, now, offline);
  if (!fresh && !pairScheduledMarginWait(pair, now, offline)) {
    add(
      'data',
      (offline
        ? '连接中断，保留最近记录。'
        : '两侧数据未齐或已超过 8 秒，当前数值仅作最近记录。') +
        (!pair.enabled && !offline
          ? pairHasPending(pair)
            ? canCheckAtStart
              ? canResumeTransfer
                ? ' 可点击“启动配对组”，服务会重新读取账户；未知划转继续保留，并预留转出金额。'
                : ' 可直接点击“启动配对组”自动核对，无需另点手动核对；结果未明时仍保持暂停。'
              : ' 仍有订单或划转待核对，暂停不会结束核对；完成后才能启动。'
            : ' 启动时服务会重新核验两侧账户、挂单及归属，再采纳实际仓位为底仓。'
          : ' 等待有效快照后才能新增开仓。'),
    );
  }
  if (pairHasPending(pair)) {
    const pendingOrder = pair.state?.pending;
    const transfer = pair.state?.margin?.pending;
    const trackedTransfer =
      !(pendingOrder && Object.keys(pendingOrder).length) &&
      transfer &&
      typeof transfer.request_id === 'string' &&
      transfer.request_id.trim() &&
      ['submitting', 'accepted', 'acknowledged', 'unknown'].includes(
        transfer.status,
      )
        ? transfer
        : null;
    const reason =
      (trackedTransfer ? pair.state?.margin?.reason : undefined) ||
      pair.state?.reason ||
      pair.pause_reason;
    const apiReason =
      reason === pairApiNotice?.text || reason === marginApiNotice?.text;
    const label = trackedTransfer
      ? pairTransferStatus(trackedTransfer)
      : pairPhaseLabel(pair.state?.phase);
    const text =
      `${label}${reason && !apiReason ? ` · ${reason}` : ''}` +
      (canCheckAtStart
        ? canResumeTransfer
          ? ' · 可重新启动；启动时重新核验账户，交易额外预留未知转出金额，原划转不重发。'
          : ' · 启动时会自动核验原单与补偿单；全部结束且账户检查通过后，保留实际平衡底仓，无需减回旧底仓。'
        : '');
    if (trackedTransfer) {
      // A retry countdown is still the same transfer and stage. Its current
      // text may change without creating another event or hiding other intents.
      notices.push({
        key: `execution:margin:${JSON.stringify([trackedTransfer.request_id, trackedTransfer.status])}`,
        kind: 'execution',
        text,
      });
    } else {
      add('execution', text);
    }
  }
  for (const [source, notice] of [
    ['pair', pairApiNotice],
    ['margin', marginApiNotice],
  ] as const) {
    if (notice)
      notices.push({
        key: `api:${source}:${notice.kind}`,
        kind: 'api',
        source,
        text: notice.text,
      });
  }
  return notices;
}

export function recordPairNotices(
  history: PairNoticeHistory | undefined,
  notices: PairNotice[],
  now: number,
): PairNoticeHistory {
  const previous = history ?? { activeKeys: [], entries: [] };
  // A missing initial server clock cannot establish an observation or recovery.
  if (!Number.isFinite(now) || now <= 0) return previous;
  const unique = [
    ...new Map(notices.map((notice) => [notice.key, notice])).values(),
  ];
  const activeKeys = unique.map((notice) => notice.key);
  const wasActive = new Set(previous.activeKeys);
  let entries = previous.entries;
  for (const notice of unique) {
    const index = entries.findIndex((entry) => entry.key === notice.key);
    const old = entries[index];
    if (old && wasActive.has(notice.key)) {
      if (
        now > old.lastSeen ||
        notice.text !== old.text ||
        notice.kind !== old.kind ||
        notice.source !== old.source
      ) {
        entries = entries.map((entry, i) =>
          i === index
            ? { ...entry, ...notice, lastSeen: Math.max(now, old.lastSeen) }
            : entry,
        );
      }
    } else {
      const lastSeen = Math.max(now, old?.lastSeen ?? now);
      const entry = {
        ...notice,
        firstSeen: old?.firstSeen ?? now,
        lastSeen,
        occurrences: (old?.occurrences ?? 0) + 1,
      };
      entries = [entry, ...entries.filter((item) => item.key !== notice.key)];
    }
  }
  // Keep the current data/execution warnings even when many older texts rotate.
  // Evicting an active entry would turn the next poll into a false recurrence.
  if (entries.length > PAIR_NOTICE_LIMIT) {
    const active = new Set(activeKeys);
    let pastSlots = Math.max(0, PAIR_NOTICE_LIMIT - active.size);
    entries = entries.filter((entry) => {
      if (active.has(entry.key)) return true;
      return pastSlots-- > 0;
    });
  }
  if (
    entries === previous.entries &&
    activeKeys.length === previous.activeKeys.length &&
    activeKeys.every((key, index) => key === previous.activeKeys[index])
  )
    return previous;
  return { activeKeys, entries };
}

export function updatePairNoticeHistories(
  previous: ReadonlyMap<string, PairNoticeHistory>,
  pairs: Pair[],
  now: number,
  offline: boolean,
): ReadonlyMap<string, PairNoticeHistory> {
  const next = new Map<string, PairNoticeHistory>();
  let changed = previous.size !== pairs.length;
  for (const pair of pairs) {
    const old = previous.get(pair.id);
    const history = recordPairNotices(
      old,
      pairStatusNotices(pair, now, offline),
      now,
    );
    next.set(pair.id, history);
    if (history !== old) changed = true;
  }
  return changed ? next : previous;
}

export function clearPastPairNotices(
  history: PairNoticeHistory | undefined,
): PairNoticeHistory {
  const previous = history ?? { activeKeys: [], entries: [] };
  const active = new Set(previous.activeKeys);
  const entries = previous.entries.filter((entry) => active.has(entry.key));
  return entries.length === previous.entries.length
    ? previous
    : { ...previous, entries };
}
