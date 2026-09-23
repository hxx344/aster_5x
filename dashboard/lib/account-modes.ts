const MODE_FIELDS = [
  { key: 'cross', label: '全仓保证金模式' },
  { key: 'hedge', label: '双向持仓模式' },
  { key: 'single_asset', label: '单币保证金模式 · USD1' },
] as const;

type ModeSnapshot = {
  timestamp?: number;
  mode_checks?: Partial<Record<(typeof MODE_FIELDS)[number]['key'], unknown>>;
};

function elapsedLabel(age: number) {
  const seconds = Math.floor(Math.max(0, age));
  if (seconds < 60) return `${seconds} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return `${Math.floor(seconds / 86400)} 天前`;
}

// A display schedule is not trading authority. Old records stay explicitly
// labelled even while the next budgeted read is not due yet.
export function accountSnapshotView(
  snapshot: { timestamp?: number } | undefined,
  now: number,
  connectionError?: string,
  refresh?: { interval_seconds: number },
  accountError?: string,
) {
  const timestamp = snapshot?.timestamp;
  const validTime =
    typeof timestamp === 'number' &&
    timestamp > 0 &&
    Number.isFinite(new Date(timestamp * 1000).getTime());
  const age = validTime ? now - timestamp! : NaN;
  const validAge = Number.isFinite(age) && age >= -1;
  const interval = refresh?.interval_seconds;
  const scheduled =
    typeof interval === 'number' && Number.isFinite(interval) && interval >= 8;
  const fresh = Boolean(
    snapshot && validTime && validAge && age < 8 && !connectionError,
  );
  // A heavily stretched shared budget must not hide minutes-old data.
  const overdue = !scheduled || age >= Math.min(interval! + 8, 120);
  const warning = Boolean(
    connectionError ||
    accountError ||
    (snapshot && (!validTime || !validAge || (!fresh && overdue))),
  );
  const notice = connectionError
    ? '连接异常，等待恢复同步'
    : accountError
      ? accountError
      : !snapshot
        ? '等待首次账户快照'
        : !validTime || !validAge
          ? '账户快照时间异常'
          : fresh
            ? ''
            : overdue
              ? '账户快照长时间未更新，请查看账户错误或执行记录'
              : `显示最近快照，按计划约每 ${Math.ceil(interval!)} 秒更新`;
  const label = connectionError
    ? '最近快照 · 连接异常'
    : accountError
      ? '最近快照 · 账户异常'
      : !snapshot
        ? '等待账户数据'
        : !validTime || !validAge
          ? '快照时间异常'
          : fresh
            ? '已同步'
            : overdue
              ? '最近快照 · 更新延迟'
              : '最近快照 · 定时更新';
  return {
    fresh,
    warning,
    label,
    notice,
    elapsed: validAge ? elapsedLabel(age) : null,
    emptyTitle: !snapshot
      ? connectionError || accountError
        ? '账户仓位读取失败'
        : '正在读取账户仓位'
      : fresh && !warning
        ? '暂无持仓'
        : '最近快照暂无持仓',
    emptyNotice: !snapshot
      ? `${connectionError || accountError ? `${notice}。` : ''}仓位会自动读取，无需启动账户。`
      : '已读取的账户快照中没有持仓；暂停期间仍会自动同步。',
  };
}

// Present the current account's retained snapshot without treating its age as
// a new mode-check result. Trading freshness is enforced separately.
export function accountModeView(
  snapshot: ModeSnapshot | undefined,
  now: number,
  connectionError?: string,
) {
  const timestamp = snapshot?.timestamp;
  const date =
    typeof timestamp === 'number' && timestamp > 0
      ? new Date(timestamp * 1000)
      : null;
  const validTime = date !== null && Number.isFinite(date.getTime());
  const age = validTime ? now - timestamp! : NaN;
  const validAge = Number.isFinite(age) && age >= -1;
  const dataNotice = !snapshot
    ? ''
    : !validTime || !validAge
      ? '账户快照时间异常'
      : age >= 8
        ? '账户数据已过期，等待刷新'
        : '';

  return {
    rows: MODE_FIELDS.map(({ key, label }) => {
      const result = snapshot?.mode_checks?.[key];
      return {
        key,
        label,
        value:
          result === true
            ? '上次核实通过'
            : result === false
              ? '不符合要求'
              : '尚未核验',
        failed: result === false,
      };
    }),
    hasSnapshot: Boolean(snapshot),
    recordedAt: validTime
      ? date.toLocaleString('zh-CN', { hour12: false })
      : null,
    elapsed: validTime && validAge ? elapsedLabel(age) : null,
    notice: [connectionError ? '连接异常' : '', dataNotice]
      .filter(Boolean)
      .join(' · '),
  };
}
