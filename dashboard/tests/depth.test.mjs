import assert from 'node:assert/strict';
import { test } from 'node:test';
import { depthQuoteView } from '../lib/depth.ts';

const sample = (overrides = {}) => ({
  timestamp: 100,
  levels_limit: 1000,
  spreads: {
    10000: {
      status: 'ok',
      spread: '0.000125',
      buy_average: '100.0125',
      sell_average: '100',
      ...overrides,
    },
  },
});

test('depth uses basis points and preserves a genuine zero', () => {
  assert.equal(depthQuoteView(sample(), 10000, 100).value, '1.25 bp');
  assert.equal(
    depthQuoteView(sample({ spread: '0' }), 10000, 100).value,
    '0.00 bp',
  );
  assert.match(depthQuoteView(sample(), 10000, 100).title, /每边分别成交/);
});

test('one insufficient side never renders a partial or zero spread', () => {
  const view = depthQuoteView(
    sample({ status: 'insufficient', spread: null, buy_average: null }),
    10000,
    100,
  );
  assert.equal(view.value, '深度不足');
  assert.equal(view.detail, '买入深度不足');
  assert.equal(view.stale, false);
});

test('missing amounts and first fetch failures have explicit states', () => {
  assert.equal(depthQuoteView(undefined, 10000, 100).value, '等待深度');
  assert.equal(depthQuoteView(sample(), 50000, 100).value, '等待深度');
  const failed = depthQuoteView(undefined, 10000, 100, '接口限流');
  assert.equal(failed.value, '获取失败');
  assert.equal(failed.detail, '接口限流');
});

test('stale and failed updates preserve the last value with a visible label', () => {
  assert.equal(depthQuoteView(sample(), 10000, 115).stale, false);
  const stale = depthQuoteView(sample(), 10000, 115.001);
  assert.equal(stale.value, '1.25 bp');
  assert.equal(stale.stale, true);
  assert.match(stale.detail, /已过期/);
  const failed = depthQuoteView(sample(), 10000, 101, '连接中断');
  assert.equal(failed.value, '1.25 bp');
  assert.equal(failed.stale, true);
  assert.match(failed.detail, /更新失败/);
  assert.match(failed.title, /连接中断/);
});

test('future timestamps and invalid values are never treated as current prices', () => {
  assert.equal(depthQuoteView(sample(), 10000, 98).stale, true);
  for (const spread of [null, 'NaN', 'Infinity', '-0.1']) {
    const view = depthQuoteView(sample({ spread }), 10000, 100);
    assert.equal(view.value, '数据无效');
    assert.equal(view.stale, true);
  }
});
