import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  PAIR_NOTICE_LIMIT,
  clearPastPairNotices,
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
