import type { NotificationChannel, State } from './desk-types';

export function notificationChannel(
  notification: State['notification'] | undefined,
  channel: 'scheduled' | 'event',
): NotificationChannel {
  return (
    notification?.channels?.[channel] ?? {
      configured: Boolean(notification?.configured),
      source: notification?.configured ? 'legacy' : 'none',
      error: notification?.error,
    }
  );
}

export function notificationOverview(notification?: State['notification']) {
  if (notification?.enabled === false) return '飞书告警已关闭';
  if (!notification?.channels)
    return (
      notification?.error ||
      (notification?.configured
        ? `飞书已配置 · ${notification.pending} 条待发送`
        : '飞书未配置')
    );
  return (['scheduled', 'event'] as const)
    .map((key) => {
      const channel = notification.channels![key];
      const name = key === 'scheduled' ? '定时' : '事件';
      return `${name}：${channel.error || (channel.configured ? '已配置' : '未配置')}`;
    })
    .join(' · ');
}
