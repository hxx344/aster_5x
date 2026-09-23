import type { CycleDiagnostic, CycleState } from './cycle';
import { isDisplayTimestamp as validTime } from './display-time.ts';

export type CycleCheckEvent = {
  symbol: string;
  phase: 'open' | 'close';
  count: number;
  first_at: number;
  last_at: number;
  diagnostic?: CycleDiagnostic | null;
};

export type ExecutionEvent = {
  id: number;
  account_id: string;
  kind: string;
  message: string;
  created_at: number;
  cycle_check?: CycleCheckEvent | null;
};

type CheckPhase = CycleCheckEvent['phase'] | 'unknown';
type LegacyCondition = {
  title: string;
  summary: string;
  phase: CheckPhase;
};

// Only these historical condition messages are safe to collapse. Old events
// have no symbol field, so their symbol must never come from current settings.
const LEGACY_CONDITIONS: LegacyCondition[] = [
  {
    title: '循环风险、余额或深度不足以满足交易所最小委托',
    summary: '循环开仓条件不足以满足最小委托',
    phase: 'open',
  },
  {
    title: '循环可执行金额低于配置的最小金额',
    summary: '循环可执行金额低于最小金额',
    phase: 'open',
  },
  {
    title: '循环价差采样金额的双边完整深度不足',
    summary: '循环采样深度不足',
    phase: 'unknown',
  },
  {
    title: '循环采样深度价差超过配置阈值（bp）',
    summary: '循环采样价差超过上限',
    phase: 'unknown',
  },
  {
    title: '循环全部平仓数量不满足交易所数量步长或限额',
    summary: '循环平仓数量不满足交易规则',
    phase: 'close',
  },
  {
    title: '循环全部平仓所需双边深度不足',
    summary: '循环平仓深度不足',
    phase: 'close',
  },
  {
    title: '循环实际平仓数量的深度价差超过配置阈值（bp）',
    summary: '循环实际平仓价差超过上限',
    phase: 'close',
  },
  {
    title: '已达到 UTC 每日成交量上限，待日额度满足后自动恢复',
    summary: '等待 UTC 日成交额度',
    phase: 'open',
  },
  {
    title: '今日剩余额度不足以完成下一轮开平仓，待 UTC 日额度满足后自动恢复',
    summary: 'UTC 日剩余额度不足下一轮',
    phase: 'open',
  },
  {
    title:
      '今日 UTC 交易量余量不足以覆盖本轮预计开仓及平仓，待 UTC 日额度满足后自动恢复',
    summary: 'UTC 日剩余额度不足下一轮',
    phase: 'open',
  },
  {
    title: '已达到滚动 24 小时成交量上限，等待历史成交移出窗口后自动重试',
    summary: '等待滚动 24 小时成交额度释放',
    phase: 'open',
  },
  {
    title:
      '已达到 UTC 每日成交量上限，待日额度和滚动 24 小时额度均满足后自动恢复',
    summary: '等待 UTC 日与滚动 24 小时成交额度',
    phase: 'open',
  },
  {
    title:
      '滚动 24 小时剩余额度不足以完成下一轮开平仓，等待历史成交移出窗口后自动重试',
    summary: '滚动 24 小时剩余额度不足下一轮',
    phase: 'open',
  },
  {
    title:
      '今日剩余额度不足以完成下一轮开平仓，待 UTC 日额度和滚动 24 小时额度均满足后自动恢复',
    summary: 'UTC 日剩余额度不足下一轮',
    phase: 'open',
  },
  {
    title:
      '今日 UTC 交易量余量不足以覆盖本轮预计开仓及平仓，待 UTC 日与滚动 24 小时额度均满足后自动恢复',
    summary: 'UTC 日剩余额度不足下一轮',
    phase: 'open',
  },
  {
    title:
      '最近 24 小时交易量余量不足以覆盖本轮预计开仓及平仓，待较早成交移出窗口且两项额度均满足后自动恢复',
    summary: '滚动 24 小时剩余额度不足下一轮',
    phase: 'open',
  },
];

// A message with an operational failure must stay visible even if an older
// service prepended a known condition title to it.
const OPERATIONAL_MESSAGE =
  /订单|回执|补救|补单|修复|鉴权|认证|签名|API|权限|提交|执行失败|请求失败|连接失败|超时|异常|报错/i;

function legacyCondition(message: string): LegacyCondition | undefined {
  if (OPERATIONAL_MESSAGE.test(message)) return undefined;
  return LEGACY_CONDITIONS.find(
    ({ title }) => message === title || message.startsWith(`${title}：`),
  );
}

function validCheck(
  check: CycleCheckEvent | null | undefined,
): check is CycleCheckEvent {
  return Boolean(
    check &&
    typeof check.symbol === 'string' &&
    check.symbol.trim() &&
    (check.phase === 'open' || check.phase === 'close') &&
    Number.isSafeInteger(check.count) &&
    check.count > 0 &&
    validTime(check.first_at) &&
    validTime(check.last_at) &&
    check.first_at <= check.last_at,
  );
}

function diagnosticTitle(
  diagnostic?: CycleDiagnostic | null,
): string | undefined {
  const title = diagnostic?.title;
  return typeof title === 'string' && title.trim() ? title : undefined;
}

export function cycleStateSummary(
  state: CycleState | undefined,
  phase: string,
  reason: string,
): string {
  if (
    !['waiting_open', 'waiting_close', 'daily_limit', 'rolling_limit'].includes(
      phase,
    ) ||
    OPERATIONAL_MESSAGE.test(reason)
  )
    return reason;
  const condition = legacyCondition(reason);
  if (condition) return condition.summary;
  const title = diagnosticTitle(state?.diagnostic);
  // Diagnostic titles summarize expected checks; unknown reasons retain their
  // full message instead of being truncated at punctuation.
  if (title && (reason === title || reason.startsWith(`${title}：`)))
    return title;
  return reason;
}

type CheckRow = {
  type: 'check';
  key: string;
  event: ExecutionEvent;
  summary: string;
  symbol: string | null;
  phase: CheckPhase;
  count: number;
  firstAt: number;
  lastAt: number;
  legacy: boolean;
  knownPhase?: CycleCheckEvent['phase'];
  diagnostic?: CycleDiagnostic | null;
};

export type ExecutionEventRow =
  | CheckRow
  | {
      type: 'event';
      key: string;
      event: ExecutionEvent;
    };

export function executionEventRows(
  events: readonly ExecutionEvent[],
): ExecutionEventRow[] {
  const rows: ExecutionEventRow[] = [];
  for (const event of events) {
    const check = event.cycle_check;
    if (event.kind === 'cycle_check') {
      if (validCheck(check) && !OPERATIONAL_MESSAGE.test(event.message)) {
        const condition = legacyCondition(event.message);
        rows.push({
          type: 'check',
          key: `check:${event.account_id}:${event.id}`,
          event,
          summary:
            condition?.summary ||
            diagnosticTitle(check.diagnostic) ||
            event.message,
          symbol: check.symbol,
          phase: check.phase,
          count: check.count,
          firstAt: check.first_at,
          lastAt: check.last_at,
          legacy: false,
          diagnostic: check.diagnostic,
        });
      } else {
        rows.push({
          type: 'event',
          key: `event:${event.account_id}:${event.id}`,
          event,
        });
      }
      continue;
    }
    const condition =
      ['wait', 'error'].includes(event.kind) && validTime(event.created_at)
        ? legacyCondition(event.message)
        : undefined;
    const previous = rows.at(-1);
    if (
      condition &&
      previous?.type === 'check' &&
      previous.legacy &&
      previous.event.account_id === event.account_id &&
      (!previous.knownPhase ||
        condition.phase === 'unknown' ||
        previous.knownPhase === condition.phase)
    ) {
      previous.count += 1;
      previous.firstAt = Math.min(previous.firstAt, event.created_at);
      previous.lastAt = Math.max(previous.lastAt, event.created_at);
      if (condition.phase === 'unknown') previous.phase = 'unknown';
      else previous.knownPhase = condition.phase;
      if (event.created_at > previous.event.created_at) {
        previous.event = event;
        previous.summary = condition.summary;
      }
      continue;
    }
    if (condition) {
      rows.push({
        type: 'check',
        key: `legacy:${event.account_id}:${event.id}`,
        event,
        summary: condition.summary,
        symbol: null,
        phase: condition.phase,
        count: 1,
        firstAt: event.created_at,
        lastAt: event.created_at,
        legacy: true,
        knownPhase: condition.phase === 'unknown' ? undefined : condition.phase,
      });
    } else {
      rows.push({
        type: 'event',
        key: `event:${event.account_id}:${event.id}`,
        event,
      });
    }
  }
  return rows;
}
