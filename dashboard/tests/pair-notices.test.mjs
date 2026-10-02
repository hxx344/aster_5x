import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  PAIR_NOTICE_LIMIT,
  clearPastPairNotices,
  pairScheduledMarginWait,
  pairStatusNotices,
  recordPairNotices,
  updatePairNoticeHistories,
} from '../lib/pair-notices.ts';

const emptyHistory = { activeKeys: [], entries: [] };
const staleText =
  '两侧数据未齐或已超过 8 秒，当前数值仅作最近记录。 等待有效快照后才能新增开仓。';
const pausedStaleText =
  '两侧数据未齐或已超过 8 秒，当前数值仅作最近记录。 启动时服务会重新核验两侧账户、挂单及归属，再采纳实际仓位为底仓。';
const offlineText = '连接中断，保留最近记录。 等待有效快照后才能新增开仓。';

test('paused unknown transfer explains reserved restart even when displayed snapshots are stale', () => {
  const fixture = pair(
    {
      margin: {
        status: 'unknown',
        blocks_trading: false,
        trading_resume_allowed: true,
        pending: { request_id: 'original', status: 'unknown' },
        reason: '已读取新的保证金基线',
      },
    },
    { enabled: false },
  );
  const text = pairStatusNotices(fixture, 120)
    .map((notice) => notice.text)
    .join('\n');
  assert.match(text, /可点击“启动配对组”/);
  assert.match(text, /预留/);
  assert.doesNotMatch(
    text,
    /完成后才能启动|结果未明时仍保持暂停|核验原单与补偿单/,
  );
});

function pair(state = {}, options = {}) {
  return {
    id: 'fixture-pair',
    enabled: true,
    ...options,
    state: {
      phase: 'waiting',
      reason: '虚构条件尚未满足',
      updated_at: 100,
      snapshots: {
        long: { timestamp: 100, positions: [] },
        short: { timestamp: 100, positions: [] },
      },
      ...state,
    },
  };
}

function notice(text, kind = 'execution') {
  return { key: `${kind}:${text}`, kind, text };
}

function marginPair() {
  return pair(
    {
      phase: 'monitoring',
      updated_at: 110,
      progress: { quantities: { LONG: '0', SHORT: '0.000' } },
      margin: {
        enabled: true,
        status: 'waiting',
        blocks_trading: false,
        checked_at: 100,
        next_check_at: 130,
        cooldown_until: 0,
        pending: null,
        api_notice: null,
      },
    },
    {
      ordinary: { enabled: false },
      cycle: { enabled: false },
      margin: { enabled: true },
    },
  );
}

test('scheduled margin waits retain source timestamps without recurring stale notices', () => {
  let history;
  for (const now of [107.9, 108, 110, 120, 130, 131]) {
    const fixture = marginPair();
    fixture.state.updated_at = now;
    const wait = pairScheduledMarginWait(fixture, now);
    assert.deepEqual(wait, {
      checkedAt: 100,
      nextCheckAt: 130,
      coolingDown: false,
      checking: now >= 130,
    });
    const notices = pairStatusNotices(fixture, now);
    assert.deepEqual(notices, []);
    history = recordPairNotices(history, notices, now);
    assert.deepEqual(history.entries, []);
    assert.equal(fixture.state.snapshots.long.timestamp, 100);
    assert.equal(fixture.state.snapshots.short.timestamp, 100);
  }
});

test('normal cooldown uses the later balance deadline and cannot wait indefinitely', () => {
  const fixture = marginPair();
  fixture.state.margin.status = 'cooldown';
  fixture.state.margin.cooldown_until = 160;
  fixture.state.updated_at = 150;
  assert.deepEqual(pairScheduledMarginWait(fixture, 150), {
    checkedAt: 100,
    nextCheckAt: 160,
    coolingDown: true,
    checking: false,
  });
  fixture.state.updated_at = 167.99;
  assert.equal(pairScheduledMarginWait(fixture, 167.99).checking, true);
  fixture.state.updated_at = 168;
  assert.equal(pairScheduledMarginWait(fixture, 168), null);
  assert.ok(
    pairStatusNotices(fixture, 168).some(({ kind }) => kind === 'data'),
  );
});

test('scheduled check grace requires a live runtime and preserves overdue or offline warnings', () => {
  for (const [now, updatedAt, offline] of [
    [118, 110, false],
    [110, 110, true],
    [138, 138, false],
  ]) {
    const fixture = marginPair();
    fixture.state.updated_at = updatedAt;
    assert.equal(pairScheduledMarginWait(fixture, now, offline), null);
    assert.ok(
      pairStatusNotices(fixture, now, offline).some(
        ({ kind }) => kind === 'data',
      ),
    );
  }
});

test('opening modes and incomplete or invalid schedules retain the existing freshness checks', () => {
  const mutations = [
    (p) => {
      p.enabled = false;
    },
    (p) => {
      p.ordinary.enabled = true;
    },
    (p) => {
      p.cycle.enabled = true;
    },
    (p) => {
      delete p.ordinary;
    },
    (p) => {
      p.margin.enabled = false;
    },
    (p) => {
      p.state.margin.enabled = false;
    },
    (p) => {
      p.state.snapshots.long = null;
    },
    (p) => {
      p.state.snapshots.short.timestamp = 112;
    },
    (p) => {
      p.state.snapshots.long.timestamp = NaN;
    },
    (p) => {
      p.state.margin.checked_at = 112;
    },
    (p) => {
      p.state.margin.next_check_at = 99;
    },
    (p) => {
      p.state.margin.next_check_at = Infinity;
    },
    (p) => {
      delete p.state.margin.next_check_at;
    },
    (p) => {
      p.state.margin.cooldown_until = NaN;
    },
    (p) => {
      p.state.margin.cooldown_until = -1;
    },
  ];
  for (const mutate of mutations) {
    const fixture = marginPair();
    mutate(fixture);
    assert.equal(pairScheduledMarginWait(fixture, 110), null);
    assert.ok(
      pairStatusNotices(fixture, 110).some(({ kind }) => kind === 'data'),
    );
  }
});

test('pending work, failed reads, API waits and remaining cycle positions are never suppressed', () => {
  const mutations = [
    (p) => {
      p.state.pending = { id: 'unresolved-order' };
    },
    (p) => {
      p.state.margin.pending = {
        request_id: 'unresolved-transfer',
        status: 'unknown',
      };
    },
    (p) => {
      p.state.margin.status = 'unknown';
    },
    (p) => {
      p.state.margin.status = 'rejected';
    },
    (p) => {
      p.state.margin.blocks_trading = true;
    },
    (p) => {
      p.state.phase = 'waiting';
    },
    (p) => {
      p.state.phase = 'attention';
    },
    (p) => {
      p.state.progress.quantities.LONG = '0.1';
    },
    (p) => {
      p.state.progress.quantities.SHORT = '1e-999';
    },
    (p) => {
      p.state.progress.quantities.SHORT = 'invalid';
    },
    (p) => {
      p.state.margin.api_notice = {
        kind: 'budget',
        text: '本地执行 API 请求权重预算不足',
      };
    },
    (p) => {
      p.state.api_notice = { kind: 'cooldown', text: '接口冷却中' };
    },
    (p) => {
      p.state.reason = '本地执行 API 请求权重预算不足';
    },
  ];
  for (const mutate of mutations) {
    const fixture = marginPair();
    mutate(fixture);
    assert.equal(pairScheduledMarginWait(fixture, 110), null);
    assert.ok(
      pairStatusNotices(fixture, 110).some(({ kind }) => kind === 'data'),
    );
  }
});

test('a genuine failure stays in history when the next healthy scheduled wait clears it', () => {
  const fixture = marginPair();
  fixture.state.phase = 'waiting';
  let history = recordPairNotices(
    undefined,
    pairStatusNotices(fixture, 110),
    110,
  );
  assert.equal(history.entries.length, 1);
  fixture.state.phase = 'monitoring';
  fixture.state.updated_at = 111;
  history = recordPairNotices(history, pairStatusNotices(fixture, 111), 111);
  assert.deepEqual(history.activeKeys, []);
  assert.equal(history.entries.length, 1);
  assert.equal(history.entries[0].occurrences, 1);
});

function freeze(value) {
  if (value && typeof value === 'object') {
    for (const child of Object.values(value)) freeze(child);
    Object.freeze(value);
  }
  return value;
}

test('fresh waiting state creates no warning and staleness does not copy ordinary waiting text', () => {
  assert.deepEqual(pairStatusNotices(pair(), 107.99), []);
  const stale = pairStatusNotices(pair(), 108);
  assert.equal(stale.length, 1);
  assert.equal(stale[0].kind, 'data');
  assert.equal(stale[0].text, staleText);
  assert.match(stale[0].key, /data/);
  assert.ok(stale[0].key.includes(staleText));
  assert.ok(!stale[0].text.includes('虚构条件尚未满足'));
});

test('runtime and both snapshots must all be fresh before data notices disappear', () => {
  for (const fixture of [
    pair({ updated_at: 92 }),
    pair({ updated_at: undefined }),
    pair({ snapshots: { long: null, short: { timestamp: 100 } } }),
    pair({ snapshots: { long: { timestamp: 100 }, short: null } }),
    pair({ snapshots: { long: { timestamp: 92 }, short: { timestamp: 100 } } }),
    pair({ snapshots: { long: { timestamp: 100 }, short: { timestamp: 92 } } }),
    pair({ updated_at: 102 }),
    pair({ updated_at: NaN }),
    { id: 'no-runtime', enabled: true },
  ]) {
    assert.deepEqual(
      pairStatusNotices(fixture, 100).map(({ kind, text }) => ({ kind, text })),
      [{ kind: 'data', text: staleText }],
    );
  }
});

test('paused and offline notices preserve their complete existing explanations', () => {
  const paused = pair({}, { enabled: false });
  assert.equal(pairStatusNotices(paused, 108)[0].text, pausedStaleText);
  assert.equal(pairStatusNotices(pair(), 100, true)[0].text, offlineText);
  assert.equal(pairStatusNotices(paused, 100, true)[0].text, offlineText);
});

test('pending execution and stale data remain two independent complete notices', () => {
  const notices = pairStatusNotices(
    pair({
      phase: 'reconciling',
      reason: '虚构订单回执待核对',
      pending: { id: 'demo-order' },
    }),
    108,
  );
  assert.equal(notices.length, 2);
  assert.equal(notices.find(({ kind }) => kind === 'data').text, staleText);
  const execution = notices.find(({ kind }) => kind === 'execution');
  assert.equal(execution.text, '订单结果核对中 · 虚构订单回执待核对');
  assert.ok(execution.key.includes(execution.text));
  assert.notEqual(
    execution.key,
    notices.find(({ kind }) => kind === 'data').key,
  );
});

test('paused unresolved orders or transfers explain the start block before adoption', () => {
  for (const pendingState of [
    { pending: { kind: 'ordinary', id: 'demo-order' } },
    { pending: { kind: 'cycle', phase: 'open', id: 'cycle-order' } },
    { pending: { kind: 'leverage', id: 'leverage-order' } },
    {
      pending: { kind: 'ordinary', phase: 'open', id: 'ordinary-order' },
      progress: { quantities: { LONG: '0.1' } },
    },
    { margin: { pending: { request_id: 'demo-transfer' } } },
  ]) {
    const notices = pairStatusNotices(
      pair({ phase: 'reconciling', ...pendingState }, { enabled: false }),
      108,
    );
    const data = notices.find(({ kind }) => kind === 'data');
    assert.match(data.text, /仍有订单或划转待核对/);
    assert.match(data.text, /完成后才能启动/);
    assert.ok(!data.text.includes('再采纳实际仓位'));
  }
});

test('paused ordinary opening offers automatic start checks and preserves balanced manual additions', () => {
  const fixture = pair(
    {
      phase: 'reconciling',
      pending: { kind: 'ordinary', phase: 'open', id: 'ordinary-order' },
      progress: { quantities: { LONG: '0', SHORT: '0' } },
    },
    { enabled: false },
  );
  for (const now of [100, 108]) {
    const notices = pairStatusNotices(fixture, now);
    const execution = notices.find(({ kind }) => kind === 'execution');
    assert.match(execution.text, /启动时会自动核验原单与补偿单/);
    assert.match(execution.text, /全部结束且账户检查通过/);
    assert.match(execution.text, /保留实际平衡底仓，无需减回旧底仓/);
    const data = notices.find(({ kind }) => kind === 'data');
    if (data) {
      assert.match(data.text, /可直接点击“启动配对组”自动核对/);
      assert.match(data.text, /结果未明时仍保持暂停/);
      assert.doesNotMatch(data.text, /完成后才能启动/);
    }
  }
  const concurrentTransfer = pairStatusNotices(
    {
      ...fixture,
      state: {
        ...fixture.state,
        margin: { pending: { request_id: 'transfer' } },
      },
    },
    108,
  );
  assert.match(
    concurrentTransfer.find(({ kind }) => kind === 'data').text,
    /完成后才能启动/,
  );
  assert.ok(
    concurrentTransfer.every(({ text }) => !text.includes('启动时会自动核验')),
  );
});

test('unknown order or transfer outcomes stay pending and use the available reason', () => {
  for (const pendingState of [
    { pending: { id: 'demo-order' } },
    { margin: { status: 'unknown' } },
    { margin: { pending: { request_id: 'demo-transfer' } } },
  ]) {
    const notices = pairStatusNotices(
      pair(
        { phase: 'unknown', reason: '', ...pendingState },
        { pause_reason: '虚构回执未确定' },
      ),
      100,
    );
    assert.equal(notices.length, 1);
    assert.equal(notices[0].kind, 'execution');
    assert.equal(notices[0].text, '结果未知 · 继续查询 · 虚构回执未确定');
  }
  const withoutReason = pairStatusNotices(
    pair({ phase: 'reconciling', reason: '', pending: { id: 'demo-order' } }),
    100,
  );
  assert.equal(withoutReason[0].text, '订单结果核对中');
  for (const phase of ['waiting', 'error', 'attention']) {
    assert.deepEqual(pairStatusNotices(pair({ phase, pending: {} }), 100), []);
  }
});

test('a short stale interval remains readable with its original text after recovery', () => {
  const stale = pairStatusNotices(pair({}, { enabled: false }), 108);
  const seen = recordPairNotices(undefined, stale, 108);
  const recovered = recordPairNotices(
    seen,
    pairStatusNotices(
      pair(
        {
          updated_at: 109,
          snapshots: {
            long: { timestamp: 109 },
            short: { timestamp: 109 },
          },
        },
        { enabled: false },
      ),
      109,
    ),
    109,
  );
  assert.deepEqual(recovered.activeKeys, []);
  assert.deepEqual(recovered.entries, [
    {
      ...stale[0],
      firstSeen: 108,
      lastSeen: 108,
      occurrences: 1,
    },
  ]);
  assert.equal(recovered.entries[0].text, pausedStaleText);
});

test('API notices are retained for both execution and margin without pending orders or transfers', () => {
  const execution = {
    kind: 'budget',
    text: '本地执行 API 请求权重预算不足，已为市场额度监控、订单核对和补偿保留请求权重',
  };
  const margin = {
    kind: 'rate_limit',
    text: '交易所请求限流，等待 12 秒后继续核对',
  };
  for (const phase of ['waiting', 'holding', 'attention', 'margin_wait']) {
    const notices = pairStatusNotices(
      pair({ phase, api_notice: execution, margin: { api_notice: margin } }),
      100,
    );
    assert.deepEqual(notices, [
      {
        key: 'api:pair:budget',
        kind: 'api',
        source: 'pair',
        text: execution.text,
      },
      {
        key: 'api:margin:rate_limit',
        kind: 'api',
        source: 'margin',
        text: margin.text,
      },
    ]);
  }
  const observed = recordPairNotices(
    undefined,
    pairStatusNotices(pair({ api_notice: execution }), 100),
    100,
  );
  const cleared = recordPairNotices(
    observed,
    pairStatusNotices(pair({ api_notice: null, reason: execution.text }), 101),
    101,
  );
  assert.deepEqual(cleared.activeKeys, []);
  assert.equal(cleared.entries[0].kind, 'api');
  assert.equal(cleared.entries[0].text, execution.text);
});

test('legacy API quota text is narrowly recognized and explicit server clearing takes precedence', () => {
  for (const text of [
    '本地 API 请求权重预算不足',
    '本地普通 API 请求权重预算不足，已为订单核对和补偿保留请求权重',
    '本地执行 API 请求权重预算不足，已为市场额度监控、订单核对和补偿保留请求权重',
  ]) {
    const old = pairStatusNotices(
      pair({ reason: text, margin: { reason: text } }),
      100,
    );
    assert.deepEqual(
      old.map(({ key }) => key),
      ['api:pair:budget', 'api:margin:budget'],
    );
    assert.ok(old.every((notice) => notice.text === text));
    assert.deepEqual(
      pairStatusNotices(
        pair({
          reason: text,
          api_notice: null,
          margin: { reason: text, api_notice: null },
        }),
        100,
      ),
      [],
    );
  }
  for (const reason of [
    '公共额度不足，等待市场额度更新',
    '本地记录的交易额度不足',
    'API 返回公共交易额度不足',
    '等待价差满足普通开仓条件',
    '本地请求预算不足',
  ]) {
    assert.deepEqual(
      pairStatusNotices(pair({ reason, margin: { reason } }), 100),
      [],
    );
  }
  for (const api_notice of [
    null,
    {},
    { kind: 'unknown', text: '未识别消息' },
    { kind: 'budget', text: { nested: 'secret' } },
    { kind: 'budget', text: '   ' },
  ]) {
    assert.deepEqual(pairStatusNotices(pair({ api_notice }), 100), []);
  }
});

test('API countdown updates keep one full latest message and count only real reappearances', () => {
  const observations = Array.from({ length: 12 }, (_, index) => {
    const text = `交易所请求冷却，剩余 ${20 - index} 秒；${'完整反馈。'.repeat(250)}`;
    return pairStatusNotices(
      pair({
        phase: 'reconciling',
        reason: text,
        api_notice: { kind: 'cooldown', text },
        pending: { id: 'demo-order' },
      }),
      100 + index / 10,
    );
  });
  let history;
  for (const [index, notices] of observations.entries()) {
    history = recordPairNotices(history, notices, 100 + index / 10);
  }
  assert.equal(history.entries.length, 2);
  const current = history.entries.find(({ kind }) => kind === 'api');
  assert.equal(current.firstSeen, 100);
  assert.equal(current.lastSeen, 101.1);
  assert.equal(current.occurrences, 1);
  assert.equal(
    current.text,
    observations.at(-1).find(({ kind }) => kind === 'api').text,
  );
  assert.equal(
    history.entries.find(({ kind }) => kind === 'execution').text,
    '订单结果核对中',
  );
  history = recordPairNotices(history, [], 102);
  history = recordPairNotices(history, observations[0], 103);
  assert.equal(
    history.entries.find(({ kind }) => kind === 'api').occurrences,
    2,
  );
  assert.equal(
    history.entries.find(({ kind }) => kind === 'api').firstSeen,
    100,
  );
  assert.equal(
    history.entries.find(({ kind }) => kind === 'api').text,
    observations[0].find(({ kind }) => kind === 'api').text,
  );
});

test('one transfer stage retains a single notice through countdown and retry changes', () => {
  let history;
  const reasons = [
    '等待下一次划转只读核对（本次检查时剩余约 4 秒）',
    '等待下一次划转只读核对（本次检查时剩余约 3 秒）',
    '交易所已确认划转，但余额刷新失败（HTTP 502）',
    '等待下一次划转只读核对（本次检查时剩余约 2 秒）',
    '等待下一次划转只读核对（本次检查时剩余约 1 秒）',
  ];
  for (let index = 0; index < reasons.length; index++) {
    const notices = pairStatusNotices(
      pair({
        phase: 'margin_wait',
        reason: '旧的普通开仓条件，不应覆盖划转诊断',
        margin: {
          pending: { request_id: 'transfer-one', status: 'acknowledged' },
          reason: reasons[index],
        },
      }),
      100 + index,
    );
    history = recordPairNotices(history, notices, 100 + index);
    assert.equal(history.entries.length, 1);
    assert.equal(history.entries[0].occurrences, 1);
    assert.equal(history.entries[0].firstSeen, 100);
    assert.equal(history.entries[0].lastSeen, 100 + index);
    assert.equal(
      history.entries[0].text,
      `交易所已确认 · 等待余额刷新 · ${reasons[index]}`,
    );
  }
  const recovered = recordPairNotices(history, [], 105);
  assert.equal(recovered.entries.length, 1);
  assert.deepEqual(recovered.activeKeys, []);
});

test('different transfers and stages remain distinct and recurrence increments once', () => {
  const observe = (request_id, status) =>
    pairStatusNotices(
      pair({
        phase: 'margin_wait',
        margin: { pending: { request_id, status }, reason: '同一段核对说明' },
      }),
      100,
    );
  const first = observe('transfer-one', 'accepted');
  const refreshed = observe('transfer-one', 'acknowledged');
  const second = observe('transfer-two', 'acknowledged');
  let history = recordPairNotices(undefined, first, 100);
  history = recordPairNotices(history, refreshed, 101);
  history = recordPairNotices(history, second, 102);
  assert.equal(history.entries.length, 3);
  assert.equal(new Set(history.entries.map(({ key }) => key)).size, 3);
  history = recordPairNotices(history, [], 103);
  history = recordPairNotices(history, second, 104);
  assert.equal(history.entries[0].occurrences, 2);
  assert.equal(history.entries[0].firstSeen, 102);
});

test('a pending order keeps its own reason when a transfer is also present', () => {
  const notices = pairStatusNotices(
    pair({
      phase: 'reconciling',
      reason: '订单查询尚未完成',
      pending: { id: 'order-one' },
      margin: {
        pending: { request_id: 'transfer-one', status: 'acknowledged' },
        reason: '划转余额刷新等待',
      },
    }),
    100,
  );
  assert.equal(notices[0].text, '订单结果核对中 · 订单查询尚未完成');
  assert.ok(!notices[0].key.startsWith('execution:margin:'));
});

test('transfer API waits remain separate from the stable transfer stage', () => {
  const text = '本地执行 API 请求权重预算不足';
  const notices = pairStatusNotices(
    pair({
      phase: 'margin_wait',
      margin: {
        pending: { request_id: 'transfer-one', status: 'acknowledged' },
        reason: text,
        api_notice: { kind: 'budget', text },
      },
    }),
    100,
  );
  assert.equal(notices.length, 2);
  assert.equal(notices[0].text, '交易所已确认 · 等待余额刷新');
  assert.equal(notices[1].key, 'api:margin:budget');
  assert.equal(notices[1].text, text);
});

test('continuous polling and repeated StrictMode updates count one occurrence', () => {
  const warning = notice('虚构结果待核对');
  const first = recordPairNotices(undefined, [warning], 100);
  const replayed = recordPairNotices(first, [warning], 100);
  const polled = recordPairNotices(replayed, [warning], 101);
  assert.deepEqual(replayed, first);
  assert.deepEqual(polled, {
    activeKeys: [warning.key],
    entries: [{ ...warning, firstSeen: 100, lastSeen: 101, occurrences: 1 }],
  });
});

test('a notice that disappears and returns increments once and moves to the front', () => {
  const first = notice('虚构提示一');
  const second = notice('虚构提示二');
  let history = recordPairNotices(undefined, [first], 100);
  history = recordPairNotices(history, [], 101);
  history = recordPairNotices(history, [second], 102);
  history = recordPairNotices(history, [first, second], 103);
  assert.deepEqual(history.activeKeys, [first.key, second.key]);
  assert.equal(history.entries[0].key, first.key);
  assert.deepEqual(
    history.entries.find(({ key }) => key === first.key),
    {
      ...first,
      firstSeen: 100,
      lastSeen: 103,
      occurrences: 2,
    },
  );
  assert.equal(
    history.entries.find(({ key }) => key === second.key).occurrences,
    1,
  );
});

test('replacing a reason retires only that exact text and leaves the new warning active', () => {
  const oldNotice = notice('虚构订单一待核对');
  const newNotice = notice('虚构订单二待核对');
  const previous = recordPairNotices(undefined, [oldNotice], 100);
  const replaced = recordPairNotices(previous, [newNotice], 101);
  assert.deepEqual(replaced.activeKeys, [newNotice.key]);
  assert.deepEqual(
    replaced.entries.map(({ text }) => text),
    [newNotice.text, oldNotice.text],
  );
  assert.equal(replaced.entries[1].lastSeen, 100);
  assert.ok(replaced.entries.every(({ occurrences }) => occurrences === 1));
});

test('invalid clocks neither insert notices nor interpret an empty poll as recovery', () => {
  const warning = notice('虚构未决状态');
  const previous = recordPairNotices(undefined, [warning], 100);
  for (const now of [0, -1, NaN, Infinity, -Infinity]) {
    assert.deepEqual(
      recordPairNotices(undefined, [warning], now),
      emptyHistory,
    );
    assert.deepEqual(recordPairNotices(previous, [], now), previous);
    assert.deepEqual(
      recordPairNotices(previous, [notice('新的虚构提示')], now),
      previous,
    );
  }
});

test('clock rollback cannot move lastSeen backwards or override occurrence ordering', () => {
  const first = notice('时钟回拨前的虚构提示');
  const second = notice('时钟回拨后的虚构提示');
  let history = recordPairNotices(undefined, [first], 100);
  history = recordPairNotices(history, [first], 90);
  assert.equal(history.entries[0].lastSeen, 100);
  history = recordPairNotices(history, [second], 91);
  assert.deepEqual(
    history.entries.map(({ key }) => key),
    [second.key, first.key],
  );
  history = recordPairNotices(history, [first], 92);
  assert.equal(history.entries[0].key, first.key);
  assert.equal(history.entries[0].firstSeen, 100);
  assert.equal(history.entries[0].lastSeen, 100);
  assert.equal(history.entries[0].occurrences, 2);
});

test('history retains the eight most recent distinct full texts without truncation', () => {
  assert.equal(PAIR_NOTICE_LIMIT, 8);
  const notices = Array.from({ length: 10 }, (_, index) =>
    notice(`虚构完整提示 ${index}：${'原因详情。'.repeat(90)}`),
  );
  let history;
  for (const [index, warning] of notices.entries()) {
    history = recordPairNotices(history, [warning], 100 + index);
  }
  assert.equal(history.entries.length, 8);
  assert.deepEqual(
    history.entries.map(({ text }) => text),
    notices
      .slice(2)
      .reverse()
      .map(({ text }) => text),
  );
  assert.deepEqual(history.activeKeys, [notices[9].key]);
});

test('changing execution reasons cannot evict or recount a continuously active data notice', () => {
  const data = notice('虚构持续数据过期提示', 'data');
  const executions = Array.from({ length: PAIR_NOTICE_LIMIT + 3 }, (_, index) =>
    notice(`虚构执行原因 ${index}`),
  );
  let history;
  for (const [index, execution] of executions.entries()) {
    const current = [data, execution];
    history = recordPairNotices(history, current, 100 + index);
    assert.deepEqual(
      history.entries.find(({ key }) => key === data.key),
      {
        ...data,
        firstSeen: 100,
        lastSeen: 100 + index,
        occurrences: 1,
      },
    );
    assert.ok(history.entries.some(({ key }) => key === execution.key));
    assert.equal(
      history.entries.length,
      Math.min(index + 2, PAIR_NOTICE_LIMIT),
    );
    const replayed = recordPairNotices(history, current, 100 + index);
    assert.deepEqual(replayed, history);
    history = replayed;
  }
  assert.deepEqual(history.activeKeys, [data.key, executions.at(-1).key]);
  assert.deepEqual(
    history.entries
      .filter(({ kind }) => kind === 'execution')
      .map(({ text }) => text),
    executions
      .slice(-(PAIR_NOTICE_LIMIT - 1))
      .reverse()
      .map(({ text }) => text),
  );
});

test('recording and clearing do not mutate histories or incoming notices', () => {
  const warning = freeze(notice('虚构不可变输入'));
  const incoming = freeze([warning]);
  const previous = freeze(recordPairNotices(undefined, incoming, 100));
  const saved = structuredClone(previous);
  const updated = recordPairNotices(previous, incoming, 101);
  assert.deepEqual(previous, saved);
  assert.equal(updated.entries[0].lastSeen, 101);
  assert.equal(warning.text, '虚构不可变输入');
  clearPastPairNotices(previous);
  assert.deepEqual(previous, saved);
});

test('all pair groups retain independent histories and deleting a group drops only its history', () => {
  const first = pair(
    { phase: 'reconciling', pending: { id: 'demo-first' } },
    { id: 'first' },
  );
  const second = pair({}, { id: 'second' });
  const original = new Map();
  const initial = updatePairNoticeHistories(
    original,
    [first, second],
    100,
    false,
  );
  assert.equal(original.size, 0);
  assert.equal(initial.get('first').entries.length, 1);
  const secondInitially = initial.get('second');
  assert.ok(!secondInitially || secondInitially.entries.length === 0);

  const switched = updatePairNoticeHistories(
    initial,
    [second, first],
    108,
    false,
  );
  assert.deepEqual(
    new Set(switched.get('first').entries.map(({ kind }) => kind)),
    new Set(['data', 'execution']),
  );
  assert.deepEqual(
    switched.get('second').entries.map(({ kind }) => kind),
    ['data'],
  );
  assert.equal(initial.get('first').entries.length, 1);
  assert.ok(
    !initial.get('second') || initial.get('second').entries.length === 0,
  );

  const removed = updatePairNoticeHistories(switched, [second], 109, false);
  assert.equal(removed.has('first'), false);
  assert.equal(removed.get('second').entries[0].occurrences, 1);
  assert.equal(switched.has('first'), true);
  assert.equal(updatePairNoticeHistories(removed, [], 110, false).size, 0);
});

test('group updates preserve active notices when the current clock is invalid', () => {
  const previous = updatePairNoticeHistories(new Map(), [pair()], 108, false);
  for (const now of [0, NaN, Infinity]) {
    const unchanged = updatePairNoticeHistories(previous, [pair()], now, false);
    assert.deepEqual(
      unchanged.get('fixture-pair'),
      previous.get('fixture-pair'),
    );
  }
});

test('clearing past notices keeps all currently active warnings and their counts', () => {
  assert.deepEqual(clearPastPairNotices(undefined), emptyHistory);
  const past = notice('虚构过去提示');
  const current = notice('虚构当前提示');
  const currentData = notice('虚构当前数据提示', 'data');
  let history = recordPairNotices(undefined, [past], 100);
  history = recordPairNotices(history, [current, currentData], 101);
  history = recordPairNotices(history, [current, currentData], 102);
  const cleared = clearPastPairNotices(history);
  assert.deepEqual(cleared.activeKeys, history.activeKeys);
  assert.deepEqual(
    cleared.entries,
    history.entries.filter(({ key }) => key !== past.key),
  );
  assert.equal(history.entries.length, 3);
  const polled = recordPairNotices(cleared, [current, currentData], 103);
  assert.ok(polled.entries.every(({ occurrences }) => occurrences === 1));
  assert.deepEqual(
    clearPastPairNotices(recordPairNotices(polled, [], 104)),
    emptyHistory,
  );
});
