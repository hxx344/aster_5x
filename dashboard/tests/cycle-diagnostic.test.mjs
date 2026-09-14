import assert from 'node:assert/strict';
import { test } from 'node:test';
import { cycleDiagnosticView } from '../lib/cycle-diagnostic.ts';

const diagnostic = {
  code: 'funding_conditions',
  title: '最小下单量同时超过保证金和可用余额限制',
  checked_at: Date.parse('2026-09-14T00:00:02Z') / 1000,
  symbol: 'XAUUSD1',
  phase: 'open',
  checks: [
    {
      code: 'margin',
      label: '预计保证金占用率',
      actual: '≈55.000000000000000001',
      required: '≤ 55',
      unit: '%',
      passed: false,
    },
    {
      code: 'balance',
      label: '可用余额',
      actual: '999.999999999999999999',
      required: '≥ 1000',
      unit: 'USD1',
      passed: false,
    },
    {
      code: 'spread',
      label: '参考深度价差',
      actual: '0.04',
      required: '≤ 0.1',
      unit: 'bp',
      passed: true,
    },
  ],
  context: [{ label: '最小下单数量', value: '0.2', unit: 'XAU' }],
  note: '所示估算来自这次检查；当前不提交新订单。',
};

test('all reported bottlenecks appear together with exact server strings and units', () => {
  const view = cycleDiagnosticView(diagnostic);
  assert.equal(view.checks.length, 3);
  assert.deepEqual(
    view.checks.map((check) => check.status),
    ['未满足', '未满足', '已满足'],
  );
  assert.equal(view.checks[0].actual, '≈55.000000000000000001');
  assert.equal(view.checks[1].actual, '999.999999999999999999');
  assert.equal(view.checks[0].required, '≤ 55');
  assert.equal(view.checks[0].unit, '%');
  assert.equal(view.checks[2].unit, 'bp');
});

test('unmet and unverified checks appear before passed checks, preserving each group order', () => {
  const [margin, balance, spread] = diagnostic.checks;
  const checks = [
    spread,
    margin,
    { ...spread, code: 'unknown', passed: null },
    balance,
  ];
  const view = cycleDiagnosticView({ ...diagnostic, checks });
  assert.deepEqual(
    view.checks.map((check) => check.label),
    [margin.label, balance.label, spread.label, spread.label],
  );
  assert.deepEqual(
    view.checks.map((check) => check.status),
    ['未满足', '未满足', '未核验', '已满足'],
  );
  assert.equal(checks[0], spread);
});

test('null or invalid verification states remain unverified and missing values never become zero', () => {
  for (const passed of [null, undefined, 'true', 1]) {
    const view = cycleDiagnosticView({
      ...diagnostic,
      checks: [
        {
          code: 'depth',
          label: '可成交深度',
          actual: null,
          required: '≥ 10000',
          unit: 'USD1',
          passed,
        },
      ],
    });
    assert.equal(view.checks[0].status, '未核验');
    assert.equal(view.checks[0].tone, 'muted');
    assert.equal(view.checks[0].actual, '—');
  }
  const empty = cycleDiagnosticView({
    ...diagnostic,
    checks: [{ actual: '', required: null, passed: false }],
  });
  assert.equal(empty.checks[0].actual, '—');
  assert.equal(empty.checks[0].required, '—');
  const zero = cycleDiagnosticView({
    ...diagnostic,
    checks: [{ actual: '0', required: '0', passed: true }],
  });
  assert.equal(zero.checks[0].actual, '0');
});

test('failure timestamps come only from diagnostic.checked_at, independent of state refresh time', () => {
  const before = cycleDiagnosticView(diagnostic);
  const refreshedState = { updated_at: diagnostic.checked_at + 60, diagnostic };
  assert.equal(
    cycleDiagnosticView(refreshedState.diagnostic).checkedAt,
    before.checkedAt,
  );
  for (const checked_at of [
    undefined,
    null,
    NaN,
    Infinity,
    -1,
    '1789344002',
    9e15,
  ])
    assert.equal(
      cycleDiagnosticView({ ...diagnostic, checked_at }).checkedAt,
      undefined,
    );
});

test('older APIs and cleared diagnoses render no retained diagnostic from another account or phase', () => {
  const original = structuredClone(diagnostic);
  assert.equal(cycleDiagnosticView(diagnostic).symbol, 'XAUUSD1');
  assert.equal(cycleDiagnosticView(undefined), null);
  assert.equal(cycleDiagnosticView(null), null);
  assert.equal(
    cycleDiagnosticView({ ...diagnostic, symbol: 'CLUSD1', phase: 'close' })
      .phase,
    '平仓',
  );
  assert.equal(cycleDiagnosticView(undefined), null);
  assert.deepEqual(diagnostic, original);
});

test('backend title, context and note survive without invented estimates or missing-field claims', () => {
  const view = cycleDiagnosticView(diagnostic);
  assert.equal(view.title, diagnostic.title);
  assert.equal(view.context[0].value, '0.2');
  assert.equal(view.context[0].unit, 'XAU');
  assert.equal(view.note, diagnostic.note);
  const unavailable = cycleDiagnosticView({
    ...diagnostic,
    title: '深度不足，无法估计',
    checks: [],
    context: [{ label: '预计占用', value: null, unit: 'USD1' }],
    note: '',
  });
  assert.equal(unavailable.checks.length, 0);
  assert.equal(unavailable.context[0].value, '—');
  assert.equal(unavailable.note, '');
});
