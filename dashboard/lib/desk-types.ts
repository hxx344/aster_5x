import type { CycleConfig, CycleState, CycleTrade } from './cycle';
import type { MigrationConfig, MigrationState } from './migration';
import type { ExecutionEvent } from './cycle-events';
import type { AccountCapacity } from './account-capacity';
import type { DepthQuote } from './depth';
export type Position = {
  symbol: string;
  side: string;
  qty: string;
  entry: string;
  mark: string;
  leverage: number;
  notional: string;
  occupied_margin: string;
  unrealized: string;
  liquidation: string | null;
};
export type Policy = {
  threshold: string;
  order_notional: string;
  margin_limit: string;
  min_open_leverage?: number;
  ordinary_symbol?: string;
  spread_limit: string;
  symbols: string[];
};
export type PolicyDraft = {
  threshold: string;
  order_notional: string;
  min_open_leverage: string;
  ordinary_symbol: string;
};
export type Account = {
  id: string;
  name: string;
  mode: string;
  env_prefix: string;
  enabled: boolean;
  deletion_block?: string | null;
  cycle_recovery_available?: boolean;
  status: string;
  reason: string;
  credential_ready: boolean;
  snapshot_refresh?: { interval_seconds: number };
  policy: Policy;
  migration?: MigrationConfig;
  migration_state?: MigrationState;
  cycle?: CycleConfig;
  cycle_state?: CycleState;
  cycle_trades?: CycleTrade[];
  cycle_trades_revision?: string;
  ordinary_add_blocks?: Record<string, string>;
  risk_limits?: {
    base: string;
    high_leverage: string;
    migration?: string;
    cycle?: string;
  };
  snapshot?: {
    equity: string;
    maintenance: string;
    occupied_margin: string;
    available: string;
    wallet: string;
    unrealized: string;
    ratio: string | null;
    margin_ratio?: string | null;
    total_notional?: string;
    timestamp: number;
    positions: Position[];
    mode_checks: { cross: boolean; hedge: boolean; single_asset: boolean };
    account_capacity?: Record<string, AccountCapacity>;
  };
  strategies: Record<
    string,
    {
      reason: string;
      phase: string;
      projected_ratio?: string;
      completed_notional?: string;
    }
  >;
};
export type Market = {
  status: string;
  error?: string;
  checked_at: number;
  capacities: Record<string, string>;
  capacity_checked_at?: Record<string, number>;
  poll_interval_ms?: number;
  fast_leverages?: number[];
  book?: {
    bid: string;
    ask: string;
    mark: string;
    spread: string;
    timestamp?: number;
  };
  book_error?: string;
  depth?: DepthQuote;
  depth_error?: string;
};
export type Listing = {
  symbol: string;
  status: string;
  onboard_at?: number | null;
  detected_at?: number;
  is_new?: boolean;
  max_leverage?: number;
  capacity?: string | null;
  remaining?: string | null;
  bracket_cap?: string | null;
  checked_at?: number | null;
  brackets_checked_at?: number;
  error?: string | null;
};
export type Listings = {
  enabled: boolean;
  monitoring_enabled?: boolean;
  discovery_enabled?: boolean;
  initialized: boolean;
  checked_at?: number | null;
  poll_seconds: number;
  stale_seconds: number;
  error?: string | null;
  rows: Record<string, Listing>;
  watched_symbols?: string[];
};
export type MonitoringSettings = {
  monitoring_enabled: boolean;
  discovery_enabled: boolean;
  auto_monitor_new: boolean;
  feishu_enabled: boolean;
  new_listing_alerts: boolean;
  strategy_capacity_alerts: boolean;
  listing_capacity_alerts: boolean;
  trade_summary_alerts: boolean;
};
export type MonitoredSymbol = {
  symbol: string;
  monitor: boolean;
  alerts: boolean;
  max_capacity_alert: boolean;
  can_watch: boolean;
  strategy_market: boolean;
  required_by: string[];
  effective_monitor: boolean;
  detail_enabled: boolean;
  status: string;
};
export type Monitoring = {
  settings: MonitoringSettings;
  revision: number;
  strategy_capacity_environment_enabled: boolean;
  symbols: MonitoredSymbol[];
};
export type State = {
  demo: boolean;
  ready: boolean;
  accounts: Account[];
  markets: Record<string, Market>;
  listings?: Listings;
  monitoring?: Monitoring;
  events: ExecutionEvent[];
  updated_at: number;
  notification: {
    configured: boolean;
    enabled?: boolean;
    pending: number;
    error?: string;
  };
};

export type DeskAction = (
  url: string,
  body?: object,
  method?: 'POST' | 'PATCH' | 'DELETE',
) => Promise<boolean>;
export type FeatureProps = {
  account: Account;
  busy: boolean;
  action: DeskAction;
  setError: (message: string) => void;
  setNotice: (message: string) => void;
};
