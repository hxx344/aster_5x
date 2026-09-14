type ConditionState = 'met' | 'unmet' | 'unknown' | 'stale';
type OrdinaryAccount = {
  policy?: {
    threshold?: string;
    margin_limit?: string;
    spread_limit?: string;
    min_open_leverage?: number;
  };
  risk_limits?: { high_leverage?: string };
  snapshot?: {
    timestamp?: number;
    ratio?: string | null;
    positions?: { symbol: string; side: string; leverage: number }[];
  };
};
type OrdinaryMarket = {
  status?: string;
  checked_at?: number;
  capacities?: Record<string, string>;
  book?: { spread?: string; timestamp?: number };
  book_error?: string;
};

function decimal(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 128) return null;
  const text = value.trim();
  const match = /^\+?(\d+(?:\.\d*)?|\.\d+)(?:[eE]([+-]?\d+))?$/.exec(text);
  return match && Math.abs(Number(match[2] || 0)) <= 100 ? text : null;
}

function compare(left: string, right: string): number {
  const parts = (value: string) => {
    const [base, exponent = '0'] = value.toLowerCase().split('e');
    const [whole, fraction = ''] = base.split('.');
    return {
      units: BigInt((whole || '0') + fraction),
      scale: Number(exponent) - fraction.length,
    };
  };
  const a = parts(left);
  const b = parts(right);
  const scale = Math.min(a.scale, b.scale);
  const difference =
    a.units * BigInt(10) ** BigInt(a.scale - scale) -
    b.units * BigInt(10) ** BigInt(b.scale - scale);
  return difference < BigInt(0) ? -1 : difference > BigInt(0) ? 1 : 0;
}

function freshness(
  timestamp: number | undefined,
  now: number,
  maximumAge: number,
  error: boolean,
): ConditionState | null {
  if (
    !timestamp ||
    timestamp <= 0 ||
    !Number.isFinite(timestamp) ||
    !Number.isFinite(now) ||
    now - timestamp < -1
  )
    return 'unknown';
  return error || now - timestamp > maximumAge ? 'stale' : null;
}

function condition(
  actual: string | null,
  required: string | null,
  operator: '>' | '<' | '≤',
  sourceState: ConditionState | null,
) {
  const comparison =
    actual !== null && required !== null ? compare(actual, required) : null;
  const state: ConditionState =
    comparison === null
      ? 'unknown'
      : sourceState ||
        ((
          operator === '>'
            ? comparison > 0
            : operator === '<'
              ? comparison < 0
              : comparison <= 0
        )
          ? 'met'
          : 'unmet');
  return {
    actual,
    required,
    operator,
    state,
    status: {
      met: '满足',
      unmet: '未满足',
      unknown: '未核验',
      stale: '已过期',
    }[state],
    tone: state === 'met' ? 'mint' : state === 'unmet' ? 'amber' : 'muted',
  };
}

export function ordinaryConditionsView(
  account: OrdinaryAccount | undefined,
  market: OrdinaryMarket | undefined,
  symbol: string,
  now: number,
  connectionError = '',
) {
  const snapshot = account?.snapshot;
  const accountState = freshness(
    snapshot?.timestamp,
    now,
    8,
    Boolean(connectionError),
  );
  const capacityState =
    market?.status === 'ok'
      ? freshness(market.checked_at, now, 8, Boolean(connectionError))
      : 'unknown';
  const bookState = market?.book_error
    ? 'unknown'
    : freshness(market?.book?.timestamp, now, 3, Boolean(connectionError));
  const ratio = decimal(snapshot?.ratio);
  const threshold = decimal(account?.policy?.threshold);
  const validLimit = (value: unknown) => {
    const result = decimal(value);
    return result !== null &&
      compare(result, '0') > 0 &&
      compare(result, '1') <= 0
      ? result
      : null;
  };
  const baseLimit = validLimit(account?.policy?.margin_limit);
  const highLimit = validLimit(
    account?.risk_limits?.high_leverage ?? account?.policy?.margin_limit,
  );
  const configuredMinimum = account?.policy?.min_open_leverage;
  const minimumLeverage =
    account && [5, 10, 20].includes(configuredMinimum ?? 5)
      ? (configuredMinimum ?? 5)
      : null;
  const long = snapshot?.positions?.find(
    (position) => position.symbol === symbol && position.side === 'LONG',
  );
  const short = snapshot?.positions?.find(
    (position) => position.symbol === symbol && position.side === 'SHORT',
  );
  const currentLeverage =
    accountState === null &&
    long &&
    short &&
    Number.isInteger(long.leverage) &&
    long.leverage > 0 &&
    long.leverage === short.leverage
      ? long.leverage
      : null;

  return {
    minimumLeverage,
    currentLeverage,
    leverageConstraint:
      currentLeverage === null || minimumLeverage === null
        ? null
        : ![5, 10, 20].includes(currentLeverage)
          ? 'unsupported'
          : currentLeverage < minimumLeverage
            ? 'below_minimum'
            : null,
    tiers: [5, 10, 20].map((leverage) => ({
      leverage,
      current: leverage === currentLeverage,
      belowMinimum: minimumLeverage !== null && leverage < minimumLeverage,
      capacity: condition(
        decimal(market?.capacities?.[leverage]),
        threshold,
        '>',
        capacityState,
      ),
      margin: condition(
        ratio,
        leverage === 5 ? baseLimit : highLimit,
        '<',
        accountState,
      ),
    })),
    spread: condition(
      decimal(market?.book?.spread),
      decimal(account?.policy?.spread_limit),
      '≤',
      bookState,
    ),
  };
}
