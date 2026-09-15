import { formatNumber } from './number-format';
export const fmt = formatNumber;
export const pct = (v?: string | number | null) =>
  v == null ? '—' : `${fmt(Number(v) * 100)}%`;
export const clock = (v?: number) =>
  v ? new Date(v * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '—';
export const symbols = ['XAUUSD1', 'SPCXUSD1', 'CLUSD1'];
export const names: Record<string, string> = {
  XAUUSD1: '黄金',
  SPCXUSD1: 'SpaceX',
  CLUSD1: '原油',
};
