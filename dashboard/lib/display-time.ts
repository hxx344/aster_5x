// Display timestamps are Unix seconds; zero is valid, unlike live quote freshness.
export function isDisplayTimestamp(value: unknown): value is number {
  return (
    typeof value === 'number' &&
    Number.isFinite(value) &&
    value >= 0 &&
    Number.isFinite(new Date(value * 1000).getTime())
  );
}
