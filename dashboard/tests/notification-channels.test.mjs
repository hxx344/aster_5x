import assert from 'node:assert/strict';
import { test } from 'node:test';
import {
  notificationChannel,
  notificationOverview,
} from '../lib/notification-channels.ts';

const notification = {
  configured: true,
  pending: 4,
  error: '定时发送失败',
  channels: {
    scheduled: {
      configured: true,
      source: 'dedicated',
      error: '定时发送失败',
      pending: 1,
    },
    event: { configured: false, source: 'none', error: null, pending: 3 },
  },
};

test('event status does not inherit the other robot configuration, pending count or error', () => {
  assert.deepEqual(
    notificationChannel(notification, 'event'),
    notification.channels.event,
  );
  assert.equal(
    notificationOverview(notification),
    '定时：定时发送失败 · 事件：未配置',
  );
});
test('both configured and master disabled have accurate overview', () => {
  const both = {
    ...notification,
    channels: {
      scheduled: { configured: true },
      event: { configured: true },
    },
  };
  assert.equal(notificationOverview(both), '定时：已配置 · 事件：已配置');
  assert.equal(
    notificationOverview({ ...both, enabled: false }),
    '飞书告警已关闭',
  );
});
test('older API retains legacy status without inventing per-channel pending counts', () => {
  const old = { configured: true, pending: 4 };
  assert.deepEqual(notificationChannel(old, 'event'), {
    configured: true,
    source: 'legacy',
    error: undefined,
  });
  assert.equal(notificationOverview(old), '飞书已配置 · 4 条待发送');
  assert.equal(notificationOverview(undefined), '飞书未配置');
});
