const formatters = new Map<number, Intl.NumberFormat>();

export function formatNumber(value?: string | number | null, digits = 2) {
  if (value == null) return '—';
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
