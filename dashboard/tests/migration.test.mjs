import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  migrationToleranceFromPercent,
  percentFromMarginLimit,
} from '../lib/policy.ts';
import { migrationStatus } from '../lib/migration.ts';

test('migration tolerance converts 0–50 percent exactly without floating-point boundary loss', () => {
  for (const [percent, ratio] of [
    ['0', '0'],
    ['5', '0.05'],
    ['50', '0.5'],
    ['0.0000000000000001', '0.000000000000000001'],
  ]) {
    assert.equal(migrationToleranceFromPercent(percent), ratio);
    assert.equal(percentFromMarginLimit(ratio), percent);
  }
  for (const value of [
    '',
    ' ',
    '-1',
    '50.0000000000000001',
    '100',
    'NaN',
    'Infinity',
    '1e200',
  ])
    assert.throws(
      () => migrationToleranceFromPercent(value),
      /迁移金额误差/,
      value,
    );
});

test('old account responses distinguish disabled, paused, and waiting without inventing progress', () => {
  assert.equal(migrationStatus(undefined, false, true).phase, 'disabled');
  assert.equal(migrationStatus(undefined, true, false).phase, 'paused');
  assert.equal(migrationStatus(undefined, true, true).phase, 'waiting');
  assert.equal(
    migrationStatus(undefined, true, true).completed_batches,
    undefined,
  );
});

test('server reconciliation and residual reasons remain visible when an account is paused', () => {
  const reconciling = migrationStatus(
    { phase: 'reconciling', reason: '正在核对已开仓目标多头' },
    true,
    false,
  );
  assert.equal(reconciling.label, '核对中');
  assert.equal(reconciling.reason, '正在核对已开仓目标多头');
  const residual = migrationStatus(
    { phase: 'residual', reason: '剩余 XAU 小于最小下单数量' },
    true,
    true,
  );
  assert.equal(residual.label, '剩余尾仓');
  assert.match(residual.reason, /小于最小下单/);
  assert.equal(
    migrationStatus({ phase: 'complete' }, true, true).label,
    '已完成',
  );
  assert.match(
    migrationStatus({ phase: 'complete' }, true, true).reason,
    /普通策略新增仍暂停/,
  );
});
