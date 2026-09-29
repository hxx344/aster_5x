import type { CapacityRelaySample, CapacityRelayStatus } from './desk-types';

type RelayTone = 'good' | 'warning' | 'muted';
type RelaySampleView = {
  key: string;
  symbol: string;
  kindLabel: string;
  sourceLabel: string;
  ageSeconds: number | null;
  ageLabel: string;
  freshness: string;
  tone: RelayTone;
};
type RelayView = {
  tone: RelayTone;
  connection: string;
  data: string;
  stale: boolean;
  note: string;
  samples: RelaySampleView[];
  wsError: string | null;
  httpError: string | null;
};

const SAMPLE_NOTE =
  '展示主服务器与副服务器之间的连接，收到消息不代表已接纳新的额度样本。';

function validTime(value: unknown): value is number {
  return (
    typeof value === 'number' &&
    value > 0 &&
    Number.isFinite(value) &&
    Number.isFinite(new Date(value * 1000).getTime())
  );
}

function elapsed(now: number, timestamp: unknown): number | null {
  return validTime(now) && validTime(timestamp) && now >= timestamp
    ? now - timestamp
    : null;
}

export function relayTime(timestamp: number | null | undefined): string {
  return validTime(timestamp)
    ? new Date(timestamp * 1000).toLocaleString('zh-CN', { hour12: false })
    : '—';
}

export function relayDuration(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds) || seconds < 0)
    return '—';
  if (seconds < 60) return `${Number(seconds.toFixed(1))} 秒`;
  if (seconds < 3600) return `${Number((seconds / 60).toFixed(1))} 分钟`;
  return `${Number((seconds / 3600).toFixed(1))} 小时`;
}

function relayError(value: unknown, transport: 'ws' | 'http'): string | null {
  if (value === null || value === undefined || value === '') return null;
  if (value === 'stream_unavailable') return 'WS 连接暂不可用';
  if (value === 'snapshot_unavailable') return 'HTTP 补取暂不可用';
  if (value === 'snapshot_invalid') return 'HTTP 快照内容无效或已过期';
  return transport === 'ws' ? 'WS 状态异常' : 'HTTP 补取异常';
}

function sampleView(
  sample: CapacityRelaySample,
  index: number,
  relay: CapacityRelayStatus,
  observationAge: number | null,
  stale: boolean,
): {
  view: RelaySampleView;
  cacheFresh: boolean;
  cycleFresh: boolean;
  isOi: boolean;
  isHttp: boolean;
} {
  const kindKnown = sample.kind === 'oi' || sample.kind === 'brackets';
  const sourceKnown = sample.source === 'ws' || sample.source === 'http';
  const age = sample.age_seconds;
  const receivedAge = validTime(relay.observed_at)
    ? elapsed(relay.observed_at, sample.received_at)
    : null;
  // Polling delay cannot establish the age of samples the server may have replaced.
  // Show the measured age at observation; stale snapshots are marked separately.
  const ageSeconds =
    observationAge !== null &&
    receivedAge !== null &&
    typeof age === 'number' &&
    Number.isFinite(age) &&
    age >= 0
      ? age
      : null;
  const limit = sample.max_age_seconds;
  const validLimit =
    typeof limit === 'number' && Number.isFinite(limit) && limit > 0;
  const maxAge = Math.min(limit, sample.kind === 'oi' ? 8 : 300);
  const known = kindKnown && sourceKnown && validLimit && ageSeconds !== null;
  const fresh = known && ageSeconds <= maxAge;
  const cycleFresh =
    sample.kind !== 'oi' || (ageSeconds !== null && ageSeconds <= 1);
  const inactive = !relay.enabled || relay.closed;
  const freshness = stale
    ? '最近记录 · 时效待更新'
    : inactive
      ? '最近记录 · 中继未启用'
      : !known
        ? '时效未知'
        : !fresh
          ? '缓存已过期'
          : sample.kind === 'brackets'
            ? '档位缓存有效'
            : cycleFresh
              ? '额度缓存有效 · 1 秒内'
              : '额度缓存有效 · 已超循环 1 秒要求';
  return {
    view: {
      key: `${sample.kind}:${sample.symbol}:${index}`,
      symbol: /^[A-Z0-9]{1,32}$/.test(sample.symbol)
        ? sample.symbol
        : '未知币种',
      kindLabel:
        sample.kind === 'oi'
          ? '公开额度'
          : sample.kind === 'brackets'
            ? '杠杆档位'
            : '未知类型',
      sourceLabel:
        sample.source === 'ws'
          ? 'WS 推送'
          : sample.source === 'http'
            ? 'HTTP 补取'
            : '未知来源',
      ageSeconds,
      ageLabel: relayDuration(ageSeconds),
      freshness:
        !stale && !inactive && known ? `采集时 · ${freshness}` : freshness,
      tone:
        stale || inactive ? 'muted' : fresh && cycleFresh ? 'good' : 'warning',
    },
    cacheFresh: !stale && !inactive && fresh,
    cycleFresh,
    isOi: sample.kind === 'oi',
    isHttp: sample.source === 'http',
  };
}

// This is display state only. The server still checks each request's own lifetime.
export function capacityRelayView(
  relay: CapacityRelayStatus | null | undefined,
  {
    now,
    updatedAt,
    connectionError,
    demo = false,
  }: {
    now: number;
    updatedAt?: number;
    connectionError?: string;
    demo?: boolean;
  },
): RelayView {
  const empty = { samples: [], wsError: null, httpError: null };
  if (demo)
    return {
      ...empty,
      tone: 'muted',
      connection: '演示模式',
      data: '无实时副服务器状态',
      stale: false,
      note: '演示数据不代表主服务器与副服务器的实际连接。',
    };

  const stateAge = elapsed(now, updatedAt);
  const stateStale =
    Boolean(connectionError) || stateAge === null || stateAge >= 8;
  if (relay === null || relay === undefined)
    return {
      ...empty,
      tone: connectionError ? 'warning' : 'muted',
      connection: `${stateStale ? '最近记录 · ' : ''}${relay === null ? '未配置副服务器' : '服务未提供状态'}`,
      data: relay === null ? '副服务器未启用' : '暂无连接详情',
      stale: stateStale,
      note: connectionError
        ? '主服务器连接异常，等待恢复状态同步。'
        : relay === null
          ? '当前主服务器未配置容量副服务器。'
          : '当前服务未提供副服务器状态；旧版服务需要升级后才能显示详情。',
    };

  const observationAge = elapsed(now, relay.observed_at);
  const stale = stateStale || observationAge === null || observationAge >= 8;
  const records = (Array.isArray(relay.samples) ? relay.samples : []).map(
    (sample, index) => sampleView(sample, index, relay, observationAge, stale),
  );
  const samples = records.map((record) => record.view);
  const legacyHttpError =
    relay.last_error === 'snapshot_unavailable' ||
    relay.last_error === 'snapshot_invalid';
  const wsError = relayError(
    relay.ws ? relay.ws.last_error : legacyHttpError ? null : relay.last_error,
    'ws',
  );
  const httpError = relayError(
    relay.http
      ? relay.http.last_error
      : legacyHttpError
        ? relay.last_error
        : null,
    'http',
  );
  const active = relay.enabled && !relay.closed;
  const freshRows = records.filter((sample) => sample.cacheFresh);
  const freshHttp = freshRows.filter((sample) => sample.isHttp).length;
  const freshOi = freshRows.filter((sample) => sample.isOi).length;
  const cycleExpired = freshRows.some(
    (sample) => sample.isOi && !sample.cycleFresh,
  );
  const retry = relay.ws?.retry_in_seconds;
  const connection = !relay.enabled
    ? '副服务器未启用'
    : relay.closed
      ? 'WS 已关闭'
      : !relay.running
        ? 'WS 未启动'
        : relay.connected
          ? 'WS 已连接'
          : typeof retry === 'number' && Number.isFinite(retry) && retry > 0
            ? 'WS 等待重连'
            : (relay.ws?.connection_attempts ?? 0) > 1 ||
                relay.ws?.disconnected_at
              ? 'WS 正在重连'
              : 'WS 正在连接';
  const httpActive = (relay.http?.inflight ?? 0) > 0;
  const data = stale
    ? samples.length
      ? `${samples.length} 项样本时效待更新`
      : '样本时效待更新'
    : !active
      ? samples.length
        ? '保留最近样本记录'
        : '未启用数据中继'
      : relay.samples === undefined
        ? '未提供样本时效'
        : !freshRows.length
          ? `${httpActive ? 'HTTP 补取中 · ' : ''}${samples.length ? '暂无有效缓存' : '等待有效样本'}`
          : `${freshHttp ? 'HTTP 补取样本有效 · ' : httpActive ? 'HTTP 补取中 · ' : ''}${freshRows.length} 项缓存有效${freshOi ? (cycleExpired ? ' · 循环额度已过期' : '') : ' · 无有效公开额度'}${freshRows.length < samples.length ? ' · 部分时效异常' : ''}`;
  const note = connectionError
    ? `主服务器连接异常，显示最近记录，无法确认当前连接和样本时效。${SAMPLE_NOTE}`
    : stale
      ? `${observationAge === null || stateAge === null ? '状态时间缺失或异常' : '状态已超过 8 秒未更新'}，显示最近记录。${SAMPLE_NOTE}`
      : SAMPLE_NOTE;
  return {
    tone: stale
      ? 'warning'
      : !active
        ? 'muted'
        : relay.running &&
            relay.connected &&
            freshOi > 0 &&
            samples.every((sample) => sample.tone === 'good') &&
            !wsError &&
            !httpError
          ? 'good'
          : 'warning',
    connection: `${stale ? '最近记录 · ' : ''}${connection}`,
    data: `${stale ? '最近记录 · ' : active && relay.samples !== undefined ? '采集时 · ' : ''}${data}`,
    stale,
    note,
    samples,
    wsError,
    httpError,
  };
}
