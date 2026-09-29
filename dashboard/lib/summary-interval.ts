export const summaryIntervalError = '请输入 1–1440 之间的整数分钟。';

export function parseSummaryInterval(value: string): number {
  const text = value.trim();
  const minutes = Number(text);
  if (!text || !Number.isInteger(minutes) || minutes < 1 || minutes > 1440)
    throw new Error(summaryIntervalError);
  return minutes;
}

export async function submitSummaryInterval({
  value,
  disabled,
  save,
}: {
  value: string;
  disabled: boolean;
  save: (body: { hourly_summary_interval_minutes: number }) => Promise<boolean>;
}): Promise<number | null> {
  if (disabled) return null;
  const minutes = parseSummaryInterval(value);
  return (await save({ hourly_summary_interval_minutes: minutes }))
    ? minutes
    : null;
}
