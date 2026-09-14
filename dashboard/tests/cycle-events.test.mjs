import assert from 'node:assert/strict';
import { test } from 'node:test';
import { cycleStateSummary, executionEventRows } from '../lib/cycle-events.ts';

const spreadTitle = '循环采样深度价差超过配置阈值（bp）';
const amountTitle = '循环风险、余额或深度不足以满足交易所最小委托';
const timestamp = Date.parse('2026-09-14T00:00:02Z') / 1000;
const reason = `${spreadTitle}：采样深度价差 实际 0.1234567890123456789 bp，要求 ≤ 0.1 bp`;
const diagnostic = {
  title: spreadTitle,
  code: 'reference_spread',
  phase: 'open',
  checked_at: timestamp,
  symbol: 'XAUUSD1',
  checks: [
    {
      code: 'spread',
      label: '采样深度价差',
      actual: '0.1234567890123456789',
      required: '≤ 0.1',
      unit: 'bp',
      passed: false,
    },
  ],
};
const event = (id, patch = {}) => ({
  id,
  account_id: 'alpha',
  kind: 'wait',
  message: reason,
  created_at: timestamp + id,
  ...patch,
});
const check = (id, patch = {}) =>
  event(id, {
    kind: 'cycle_check',
    cycle_check: {
      symbol: 'XAUUSD1',
      phase: 'open',
      count: 120,
      first_at: timestamp - 120,
      last_at: timestamp + id,
      diagnostic,
    },
    ...patch,
  });

test('server aggregation retains the authoritative count, times and full latest decimal strings', () => {
  const source = check(100);
  const rows = executionEventRows([source]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].type, 'check');
  assert.equal(rows[0].count, 120);
  assert.equal(rows[0].firstAt, timestamp - 120);
  assert.equal(rows[0].lastAt, timestamp + 100);
  assert.equal(rows[0].summary, '循环采样价差超过上限');
  assert.equal(rows[0].event.message, reason);
  assert.equal(rows[0].diagnostic.checks[0].actual, '0.1234567890123456789');
  assert.equal(rows[0].legacy, false);
});

test('server groups keep separate IDs across accounts, symbols, phases and intervening control events', () => {
  const original = check(5);
  const rows = executionEventRows([
    original,
    check(4, { cycle_check: { ...original.cycle_check, symbol: 'CLUSD1' } }),
    check(3, { cycle_check: { ...original.cycle_check, phase: 'close' } }),
    check(2, { account_id: 'beta' }),
    event(1, { kind: 'control', message: '账户已暂停' }),
    check(0),
  ]);
  assert.equal(rows.length, 6);
  assert.equal(new Set(rows.map((row) => row.key)).size, 6);
  assert.equal(rows[4].type, 'event');
});

test('refreshing the same server group preserves its render key while updating the latest values', () => {
  const before = check(5);
  const after = check(5, {
    message: `${amountTitle}：可用余额 实际 9.999999999999999999 USD1，要求 ≥ 10 USD1`,
    created_at: timestamp + 200,
    cycle_check: {
      ...before.cycle_check,
      count: 121,
      last_at: timestamp + 200,
      diagnostic: { ...diagnostic, title: amountTitle },
    },
  });
  const [oldRow] = executionEventRows([before]);
  const [newRow] = executionEventRows([after]);
  assert.equal(newRow.key, oldRow.key);
  assert.equal(newRow.count, 121);
  assert.equal(newRow.lastAt, timestamp + 200);
  assert.equal(newRow.firstAt, oldRow.firstAt);
  assert.match(newRow.event.message, /9\.999999999999999999/);
});

test('adjacent historical checks retain the newest detail when collapsed', () => {
  const source = [
    event(3),
    event(2, { kind: 'error', message: `${spreadTitle}：实际 0.2 bp` }),
    event(1, { message: `${spreadTitle}：实际 0.3 bp` }),
  ];
  const original = structuredClone(source);
  const [row] = executionEventRows(source);
  assert.equal(row.count, 3);
  assert.equal(row.symbol, null);
  assert.equal(row.phase, 'unknown');
  assert.equal(row.firstAt, timestamp + 1);
  assert.equal(row.lastAt, timestamp + 3);
  assert.equal(row.event.message, reason);
  assert.equal(row.legacy, true);
  assert.deepEqual(source, original);
});

test('each unrecognized, control, fill, repair or global event breaks a historical group', () => {
  for (const divider of [
    event(2, { kind: 'control', message: '账户已启动' }),
    event(2, { kind: 'fill', message: '普通加仓已成交' }),
    event(2, { kind: 'repair', message: '循环补单已成交' }),
    event(2, { kind: 'wait', message: '普通策略等待行情' }),
    event(2, { kind: 'error', message: '认证失败，需要核对 API 权限' }),
    event(2, { account_id: '', kind: 'capacity', message: '额度提醒已发送' }),
  ]) {
    const rows = executionEventRows([event(3), divider, event(1)]);
    assert.equal(rows.length, 3);
    assert.equal(rows[0].count, 1);
    assert.equal(rows[1].type, 'event');
    assert.equal(rows[1].event.message, divider.message);
    assert.equal(rows[2].count, 1);
  }
});

test('historical accounts, known phase conflicts and server symbol metadata are never crossed', () => {
  const rows = executionEventRows([
    check(6),
    event(5),
    event(4, { account_id: 'beta' }),
    event(3, { message: `${amountTitle}：实际 99` }),
    event(2, { message: '循环全部平仓所需双边深度不足：实际 0.1' }),
    event(1),
  ]);
  assert.equal(rows.length, 5);
  assert.equal(rows[1].symbol, null);
  assert.equal(rows[1].phase, 'unknown');
  assert.equal(rows[3].phase, 'open');
  assert.equal(rows[4].phase, 'unknown');
  assert.equal(rows[4].knownPhase, 'close');
  assert.equal(rows[4].count, 2);
  assert.ok(rows.every((row) => row.type === 'check'));
});

test('alternating historical condition titles merge with an unknown phase without guessing its meaning', () => {
  const rows = executionEventRows([
    event(5),
    event(4, { message: `${amountTitle}：实际 99` }),
    event(3),
    event(2, { message: '循环可执行金额低于配置的最小金额：实际 0.1' }),
    event(1),
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].count, 5);
  assert.equal(rows[0].phase, 'unknown');
  assert.equal(rows[0].knownPhase, 'open');
  assert.equal(rows[0].summary, '循环采样价差超过上限');
  assert.equal(rows[0].event.message, reason);
});

test('unknown checks never bridge contradictory known open and close phases', () => {
  const openMessage = `${amountTitle}：实际 99`;
  const closeMessage = '循环全部平仓所需双边深度不足：实际 0.1';
  for (const [first, last] of [
    [openMessage, closeMessage],
    [closeMessage, openMessage],
  ]) {
    const rows = executionEventRows([
      event(5),
      event(4, { message: first }),
      event(3),
      event(2, { message: last }),
      event(1),
    ]);
    assert.equal(rows.length, 2);
    assert.deepEqual(
      rows.map((row) => row.count),
      [3, 2],
    );
    assert.deepEqual(
      rows.map((row) => row.phase),
      ['unknown', 'unknown'],
    );
    assert.notEqual(rows[0].knownPhase, rows[1].knownPhase);
  }
});

test('only full approved condition titles or a Chinese-colon suffix are historical checks', () => {
  const titles = [
    amountTitle,
    '循环可执行金额低于配置的最小金额',
    '循环价差采样金额的双边完整深度不足',
    spreadTitle,
    '循环全部平仓数量不满足交易所数量步长或限额',
    '循环全部平仓所需双边深度不足',
    '循环实际平仓数量的深度价差超过配置阈值（bp）',
    '已达到滚动 24 小时成交量上限，等待历史成交移出窗口后自动重试',
    '已达到 UTC 每日成交量上限，待日额度和滚动 24 小时额度均满足后自动恢复',
    '滚动 24 小时剩余额度不足以完成下一轮开平仓，等待历史成交移出窗口后自动重试',
    '今日剩余额度不足以完成下一轮开平仓，待 UTC 日额度和滚动 24 小时额度均满足后自动恢复',
    '今日 UTC 交易量余量不足以覆盖本轮预计开仓及平仓，待 UTC 日与滚动 24 小时额度均满足后自动恢复',
    '最近 24 小时交易量余量不足以覆盖本轮预计开仓及平仓，待较早成交移出窗口且两项额度均满足后自动恢复',
  ];
  for (const title of titles) {
    for (const message of [title, `${title}：实际 0.123456789`])
      assert.equal(
        executionEventRows([event(1, { message })])[0].type,
        'check',
      );
    for (const message of [
      `${title}但是原因未知`,
      `${title}: 非约定格式`,
      `前缀${title}`,
    ])
      assert.equal(
        executionEventRows([event(1, { message })])[0].type,
        'event',
      );
  }
});

test('operational details stay fully visible even after an approved condition title', () => {
  for (const failure of [
    '订单回执未知',
    '补救失败',
    '补单失败',
    '修复中',
    '鉴权失败',
    '认证失败',
    '签名无效',
    'API 权限不足',
    '提交失败',
    '执行失败',
    '请求失败',
    '连接失败',
    '超时',
    '异常',
    '报错',
  ]) {
    const message = `${spreadTitle}：${failure}，需要核对`;
    for (const source of [event(1, { message }), check(1, { message })]) {
      const [row] = executionEventRows([source]);
      assert.equal(row.type, 'event');
      assert.equal(row.event.message, message);
    }
  }
});

test('invalid server metadata remains an ordinary full row, never a legacy aggregate', () => {
  const base = check(1);
  for (const metadata of [
    undefined,
    null,
    {},
    { ...base.cycle_check, count: 0 },
    { ...base.cycle_check, count: 1.5 },
    { ...base.cycle_check, count: Number.MAX_SAFE_INTEGER + 1 },
    { ...base.cycle_check, symbol: '' },
    { ...base.cycle_check, phase: 'unknown' },
    { ...base.cycle_check, first_at: timestamp + 99 },
    { ...base.cycle_check, last_at: Infinity },
    { ...base.cycle_check, first_at: '1789344002' },
    { ...base.cycle_check, first_at: -1 },
  ]) {
    const [row] = executionEventRows([check(1, { cycle_check: metadata })]);
    assert.equal(row.type, 'event');
    assert.equal(row.event.message, reason);
  }
});

test('recognized waiting summaries are short while diagnostic numbers remain in the event', () => {
  assert.equal(
    cycleStateSummary({ diagnostic }, 'waiting_open', reason),
    '循环采样价差超过上限',
  );
  assert.equal(
    cycleStateSummary(undefined, 'waiting_close', reason),
    '循环采样价差超过上限',
  );
  const custom = { ...diagnostic, title: '最小下单量超过可用余额' };
  assert.equal(
    cycleStateSummary(
      { diagnostic: custom },
      'waiting_open',
      `${custom.title}：实际 0.0001`,
    ),
    custom.title,
  );
  assert.equal(executionEventRows([check(1)])[0].event.message, reason);
});

test('attention, unknown failures, stale diagnoses and manually paused statuses retain their full reason', () => {
  for (const phase of [
    'attention',
    'error',
    'paused',
    'disabled',
    'reconciling',
  ])
    assert.equal(cycleStateSummary({ diagnostic }, phase, reason), reason);
  for (const message of [
    '新错误类型：必须核对批次 123',
    `${spreadTitle}：订单提交失败`,
    '账户已手动暂停，额度释放后仍需手动启动',
  ])
    assert.equal(
      cycleStateSummary({ diagnostic }, 'waiting_open', message),
      message,
    );
});

test('legacy aggregation happens before display limits so repeated checks do not hide earlier fills', () => {
  const events = Array.from({ length: 80 }, (_, index) => event(100 - index));
  events.push(event(10, { kind: 'fill', message: '普通加仓已成交' }));
  const rows = executionEventRows(events).slice(0, 50);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].count, 80);
  assert.equal(rows[1].event.message, '普通加仓已成交');
});
