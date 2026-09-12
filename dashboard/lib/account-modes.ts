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
