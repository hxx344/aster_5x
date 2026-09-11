export const SUPPORTED_LEVERAGES: readonly number[] = [5, 10, 20];

// Shift decimal strings without introducing binary floating-point rounding.
function shiftDecimal(value: string, places: number): string {
  const text = value.trim();
  const match = /^\+?(\d+(?:\.\d*)?|\.\d+)(?:[eE]([+-]?\d+))?$/.exec(text);
  if (!match || text.length > 128)
    throw new Error('请输入有效的风险约束百分比');
  const exponent = Number(match[2] || 0);
  if (Math.abs(exponent) > 100) throw new Error('风险约束精度超出范围');
  const [whole, fraction = ''] = match[1].split('.');
  const digits = (whole + fraction).replace(/^0+/, '') || '0';
  if (digits === '0') return '0';
  const point = digits.length + exponent + places - fraction.length;
  const shifted =
    point <= 0
      ? `0.${'0'.repeat(-point)}${digits}`
      : point >= digits.length
        ? digits + '0'.repeat(point - digits.length)
        : `${digits.slice(0, point)}.${digits.slice(point)}`;
  const result = shifted.includes('.')
    ? shifted.replace(/0+$/, '').replace(/\.$/, '')
    : shifted;
  if (result.length > 128 || (result.split('.')[1]?.length || 0) > 100)
    throw new Error('风险约束精度超出范围');
  return result;
}

export function marginLimitFromPercent(percent: string): string {
  const ratio = shiftDecimal(percent, -2);
  if (ratio === '0' || (ratio !== '1' && !ratio.startsWith('0.')))
    throw new Error('风险约束必须大于 0%，且不超过 100%');
  return ratio;
}

export function percentFromMarginLimit(ratio: string): string {
  return shiftDecimal(ratio, 2);
}

export function parseMinimumLeverage(value: string): number {
  let normalized: string;
  try {
    normalized = shiftDecimal(value, 0);
  } catch {
    throw new Error('最低开仓杠杆只能选择 5x、10x 或 20x');
  }
  const minimum = /^\d+$/.test(normalized) ? Number(normalized) : NaN;
  if (!SUPPORTED_LEVERAGES.includes(minimum))
    throw new Error('最低开仓杠杆只能选择 5x、10x 或 20x');
  return minimum;
}
