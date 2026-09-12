export type MigrationConfig = {
  enabled: boolean;
  spread_limit_bp: string;
  batch_notional: string;
  notional_tolerance: string;
};

export type MigrationDraft = {
  enabled: boolean;
  spread_limit_bp: string;
  batch_notional: string;
  tolerance_percent: string;
};

type Sides = Partial<Record<'LONG' | 'SHORT', string>>;

export type MigrationState = {
  phase?: string;
  reason?: string;
  run_id?: string;
  started_at?: number;
  updated_at?: number;
  target_symbol?: string | null;
  required_leverage?: number | null;
  target_leverage?: number | null;
  source_initial_qty?: Sides;
  source_remaining_qty?: Sides;
  migrated_notional?: Sides;
  cumulative_notional_delta?: Sides;
  completed_batches?: number;
  active_batch?: {
    stage?: string;
    source_symbol?: string;
    target_symbol?: string;
  } | null;
};

const PHASES: Record<string, { label: string; reason: string }> = {
  disabled: { label: '未开启', reason: '迁移功能已关闭' },
  paused: { label: '已暂停', reason: '启动账户后继续迁移，已有进度保留' },
  waiting: { label: '等待条件', reason: '等待核对额度、深度与可用保证金' },
  executing: { label: '迁移中', reason: '正在执行本批迁移' },
  reconciling: { label: '核对中', reason: '正在核对本批成交，暂不开始下一批' },
  attention: { label: '需要处理', reason: '请核对未完成批次后继续' },
  complete: {
    label: '已完成',
    reason: '本轮 XAU 仓位已迁移，普通策略新增仍暂停',
  },
  residual: {
    label: '剩余尾仓',
    reason: '剩余仓位暂不满足交易规则，请查看具体原因',
  },
};

export function migrationStatus(
  state: MigrationState | undefined,
  enabled: boolean,
  accountEnabled: boolean,
) {
  const phase =
    state?.phase ||
    (!enabled ? 'disabled' : accountEnabled ? 'waiting' : 'paused');
  const status = PHASES[phase] || {
    label: '等待状态',
    reason: '等待更新迁移状态',
  };
  return { phase, label: status.label, reason: state?.reason || status.reason };
}
