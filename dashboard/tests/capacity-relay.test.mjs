import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  capacityRelayView,
  relayDuration,
  relayTime,
} from '../lib/capacity-relay.ts';

const timestamp = 1_800_000_000;
const options = { now: timestamp, updatedAt: timestamp };
const sample = (values = {}) => ({
  kind: 'oi',
  symbol: 'XAUUSD1',
  source: 'ws',
  age_seconds: 0.2,
  max_age_seconds: 8,
  received_at: timestamp - 0.1,
  ...values,
});
const status = (values = {}) => ({
  enabled: true,
  running: true,
  connected: true,
  closed: false,
  cached_samples: 1,
  last_error: null,
  observed_at: timestamp,
  ws: {
    connected_at: timestamp - 60,
    disconnected_at: null,
    last_message_at: timestamp - 0.1,
    last_sample_at: timestamp - 0.1,
    connection_attempts: 1,
    retry_in_seconds: null,
    last_error: null,
  },
  http: {
    inflight: 0,
    requests: 0,
    failures: 0,
    last_attempt_at: null,
    last_success_at: null,
    last_error: null,
  },
  samples: [sample()],
  ...values,
});

test('unconfigured, unavailable legacy state and demo are distinct', () => {
  const unconfigured = capacityRelayView(null, options);
  const unsupported = capacityRelayView(undefined, options);
  const demo = capacityRelayView(status(), { ...options, demo: true });
  assert.equal(unconfigured.connection, '未配置副服务器');
  assert.equal(unsupported.connection, '服务未提供状态');
  assert.equal(demo.connection, '演示模式');
  assert.equal(demo.samples.length, 0);
  for (const view of [unconfigured, unsupported, demo])
    assert.equal(view.tone, 'muted');
});

test('legacy basic connection fields cannot establish sample freshness', () => {
  const view = capacityRelayView(
    status({
      observed_at: undefined,
      ws: undefined,
      http: undefined,
      samples: undefined,
    }),
    options,
  );
  assert.equal(view.stale, true);
  assert.equal(view.connection, '最近记录 · WS 已连接');
  assert.notEqual(view.tone, 'good');
  assert.match(view.note, /时间缺失或异常/);
  assert.equal(view.samples.length, 0);
});

test('WS connection, received messages and accepted samples are separate facts', () => {
  const view = capacityRelayView(
    status({
      cached_samples: 0,
      samples: [],
      ws: { ...status().ws, last_sample_at: null },
    }),
    options,
  );
  assert.equal(view.connection, 'WS 已连接');
  assert.equal(view.data, '等待有效样本');
  assert.notEqual(view.tone, 'good');
  const expired = capacityRelayView(
    status({ samples: [sample({ age_seconds: 9 })] }),
    options,
  );
  assert.equal(expired.connection, 'WS 已连接');
  assert.equal(expired.samples[0].freshness, '缓存已过期');
  assert.notEqual(expired.tone, 'good');
});

test('a current accepted OI sample displays its transport without claiming full coverage', () => {
  const view = capacityRelayView(status(), options);
  assert.equal(view.tone, 'good');
  assert.equal(view.data, '1 项缓存有效');
  assert.equal(view.samples[0].sourceLabel, 'WS 推送');
  assert.equal(view.samples[0].kindLabel, '公开额度');
  assert.equal(view.samples[0].ageSeconds, 0.2);
  assert.match(view.note, /收到消息不代表已接纳/);
  assert.doesNotMatch(view.data, /全部|市场正常/);
});

test('HTTP activity or successful response alone does not prove usable HTTP data', () => {
  for (const http of [
    { ...status().http, inflight: 1, requests: 1, last_attempt_at: timestamp },
    {
      ...status().http,
      requests: 1,
      last_attempt_at: timestamp,
      last_success_at: timestamp,
    },
  ]) {
    const view = capacityRelayView(
      status({ connected: false, samples: [], http }),
      options,
    );
    assert.doesNotMatch(view.data, /样本有效|缓存有效/);
    assert.notEqual(view.tone, 'good');
    if (http.inflight) assert.match(view.data, /HTTP 补取中/);
  }
  const retainedWs = capacityRelayView(
    status({ http: { ...status().http, inflight: 1 } }),
    options,
  );
  assert.match(retainedWs.data, /HTTP 补取中/);
  assert.doesNotMatch(retainedWs.data, /HTTP 补取样本有效/);
});

test('fresh HTTP samples can remain usable while WS is awaiting reconnection', () => {
  const view = capacityRelayView(
    status({
      connected: false,
      samples: [sample({ source: 'http' })],
      ws: {
        ...status().ws,
        retry_in_seconds: 3,
        disconnected_at: timestamp,
        last_error: 'stream_unavailable',
      },
      http: { ...status().http, requests: 1, last_success_at: timestamp },
    }),
    options,
  );
  assert.equal(view.connection, 'WS 等待重连');
  assert.match(view.data, /HTTP 补取样本有效/);
  assert.equal(view.wsError, 'WS 连接暂不可用');
  assert.equal(view.httpError, null);
  assert.equal(view.tone, 'warning');
});

test('HTTP errors remain visible independently of a healthy WS stream', () => {
  const view = capacityRelayView(
    status({
      http: { ...status().http, failures: 1, last_error: 'snapshot_invalid' },
    }),
    options,
  );
  assert.equal(view.connection, 'WS 已连接');
  assert.equal(view.wsError, null);
  assert.equal(view.httpError, 'HTTP 快照内容无效或已过期');
  assert.notEqual(view.tone, 'good');
});

test('an explicit cleared transport error overrides a retained legacy error', () => {
  const httpRecovered = capacityRelayView(
    status({
      last_error: 'snapshot_unavailable',
      http: {
        ...status().http,
        requests: 2,
        failures: 1,
        last_attempt_at: timestamp,
        last_success_at: timestamp,
        last_error: null,
      },
    }),
    options,
  );
  assert.equal(httpRecovered.httpError, null);
  assert.equal(httpRecovered.wsError, null);
  assert.equal(httpRecovered.tone, 'good');
  assert.doesNotMatch(httpRecovered.data, /HTTP 补取样本有效/);
  const wsRecovered = capacityRelayView(
    status({ last_error: 'stream_unavailable' }),
    options,
  );
  assert.equal(wsRecovered.wsError, null);
  assert.equal(wsRecovered.tone, 'good');
});

test('elapsed server time ages retained samples without renewing their timestamps', () => {
  const relay = status();
  const before = structuredClone(relay);
  const view = capacityRelayView(relay, {
    now: timestamp + 2,
    updatedAt: timestamp,
  });
  assert.equal(view.samples[0].ageSeconds, 2.2);
  assert.match(view.samples[0].freshness, /缓存有效.*已超循环 1 秒/);
  assert.match(view.data, /循环额度已过期/);
  assert.equal(view.samples[0].tone, 'warning');
  assert.equal(view.tone, 'warning');
  assert.deepEqual(relay, before);
});

test('OI cache lifetime and the shorter cycle lifetime keep their own boundaries', () => {
  for (const [age, cacheValid, cycleValid] of [
    [1, true, true],
    [1.01, true, false],
    [8, true, false],
    [8.01, false, false],
  ]) {
    const view = capacityRelayView(
      status({ samples: [sample({ age_seconds: age })] }),
      options,
    );
    assert.equal(view.samples[0].freshness.includes('缓存有效'), cacheValid);
    assert.equal(view.samples[0].tone === 'good', cycleValid);
  }
});

test('bracket freshness does not imply current OI or cycle readiness', () => {
  const relay = status({
    samples: [
      sample({ kind: 'brackets', age_seconds: 300, max_age_seconds: 300 }),
    ],
  });
  const view = capacityRelayView(relay, options);
  assert.equal(view.samples[0].freshness, '档位缓存有效');
  assert.match(view.data, /无有效公开额度/);
  assert.notEqual(view.tone, 'good');
  const expired = capacityRelayView(relay, {
    now: timestamp + 0.5,
    updatedAt: timestamp,
  });
  assert.equal(expired.samples[0].freshness, '缓存已过期');
});

test('status ages of eight seconds or an API connection error suppress all green states', () => {
  for (const update of [
    { now: timestamp + 8, updatedAt: timestamp },
    { now: timestamp + 8, updatedAt: timestamp + 8 },
    {
      ...options,
      connectionError: 'secret https://relay.example?token=hidden',
    },
  ]) {
    const view = capacityRelayView(
      status({ samples: [sample({ kind: 'brackets', max_age_seconds: 300 })] }),
      update,
    );
    assert.equal(view.stale, true);
    assert.equal(view.tone, 'warning');
    assert.match(view.connection, /最近记录/);
    assert.match(view.data, /最近记录/);
    assert.equal(view.samples[0].tone, 'muted');
    assert.doesNotMatch(JSON.stringify(view), /secret|relay.example|hidden/);
  }
  assert.equal(
    capacityRelayView(status(), {
      now: timestamp + 7.999,
      updatedAt: timestamp,
    }).stale,
    false,
  );
});

test('unknown, nonfinite and future observation or API timestamps never appear current', () => {
  for (const value of [
    undefined,
    null,
    NaN,
    Infinity,
    -Infinity,
    0,
    -1,
    timestamp + 0.01,
    8.64e12 + 1,
  ]) {
    for (const view of [
      capacityRelayView(status({ observed_at: value }), options),
      capacityRelayView(status(), { ...options, updatedAt: value }),
    ]) {
      assert.equal(view.stale, true);
      assert.notEqual(view.tone, 'good');
      assert.notEqual(view.samples[0].tone, 'good');
    }
  }
  assert.equal(
    capacityRelayView(status(), { ...options, now: NaN }).stale,
    true,
  );
});

test('invalid sample age, receive time, kind, source or lifetime cannot be fresh', () => {
  const changes = [
    ...[null, undefined, NaN, Infinity, -1].map((age_seconds) => ({
      age_seconds,
    })),
    ...[null, undefined, NaN, Infinity, 0, timestamp + 0.01].map(
      (received_at) => ({ received_at }),
    ),
    ...[null, undefined, NaN, Infinity, 0, -1].map((max_age_seconds) => ({
      max_age_seconds,
    })),
    { kind: 'unknown' },
    { source: 'unknown' },
  ];
  for (const values of changes) {
    const view = capacityRelayView(
      status({ samples: [sample(values)] }),
      options,
    );
    assert.equal(view.samples[0].freshness, '时效未知');
    assert.notEqual(view.tone, 'good');
    assert.doesNotMatch(view.data, /缓存有效/);
  }
  const enlarged = capacityRelayView(
    status({ samples: [sample({ age_seconds: 9, max_age_seconds: 300 })] }),
    options,
  );
  assert.equal(enlarged.samples[0].freshness, '缓存已过期');
});

test('stopped and closed transports are never presented as connected', () => {
  for (const [values, label] of [
    [{ enabled: false }, '副服务器未启用'],
    [{ closed: true }, 'WS 已关闭'],
    [{ running: false }, 'WS 未启动'],
    [{ connected: false, ws: undefined }, 'WS 正在连接'],
    [
      { connected: false, ws: { ...status().ws, connection_attempts: 2 } },
      'WS 正在重连',
    ],
  ]) {
    const view = capacityRelayView(status(values), options);
    assert.equal(view.connection, label);
    assert.notEqual(view.tone, 'good');
    if (values.enabled === false || values.closed) {
      assert.doesNotMatch(view.data, /缓存有效/);
      assert.equal(view.samples[0].tone, 'muted');
    }
  }
});

test('unknown errors never echo credentials, URLs or transport exception text', () => {
  const secret = 'https://relay.example:8443/stream?token=my-secret-token';
  const view = capacityRelayView(
    status({
      last_error: secret,
      ws: { ...status().ws, last_error: secret },
      http: { ...status().http, last_error: secret },
    }),
    options,
  );
  assert.equal(view.wsError, 'WS 状态异常');
  assert.equal(view.httpError, 'HTTP 补取异常');
  assert.doesNotMatch(JSON.stringify(view), /relay.example|my-secret-token/);
  const legacyHttp = capacityRelayView(
    status({
      last_error: 'snapshot_unavailable',
      ws: undefined,
      http: undefined,
    }),
    options,
  );
  assert.equal(legacyHttp.wsError, null);
  assert.equal(legacyHttp.httpError, 'HTTP 补取暂不可用');
});

test('display helpers handle missing and invalid durations and dates', () => {
  for (const value of [null, undefined, NaN, Infinity, -1]) {
    assert.equal(relayDuration(value), '—');
    assert.equal(relayTime(value), '—');
  }
  assert.equal(relayTime(0), '—');
  assert.equal(relayTime(8.64e12 + 1), '—');
  assert.notEqual(relayTime(timestamp), '—');
  assert.equal(relayDuration(0), '0 秒');
  assert.equal(relayDuration(0.24), '0.2 秒');
  assert.equal(relayDuration(300), '5 分钟');
});
