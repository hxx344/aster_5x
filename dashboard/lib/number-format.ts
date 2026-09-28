const formatters = new Map<number, Intl.NumberFormat>();

export function formatNumber(value?: string | number | null, digits = 2) {
  if (
    value == null ||
    (typeof value === 'string' && value.trim() === '') ||
    !Number.isFinite(Number(value))
  )
    return '—';
  let formatter = formatters.get(digits);
  if (!formatter) {
    formatter = new Intl.NumberFormat('en-US', {
      maximumFractionDigits: digits,
      minimumFractionDigits: digits,
    });
    formatters.set(digits, formatter);
  }
  return formatter.format(Number(value));
}

export function formatCount(value?: number | null) {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0
    ? formatNumber(value, 0)
    : '—';
}

export function formatPercent(value?: string | number | null) {
  if (formatNumber(value) === '—') return '—';
  const percent = formatNumber(Number(value) * 100);
  return percent === '—' ? '—' : `${percent}%`;
}
