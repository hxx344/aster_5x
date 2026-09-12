'use client';

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Activity,
  ArrowDownLeft,
  ArrowUpRight,
  Bell,
  CirclePause,
  CirclePlay,
  Layers3,
  LogOut,
  Plus,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  Wallet,
} from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Switch } from '@/components/ui/switch';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import { Progress } from '@/components/ui/progress';
import { createStatePoller } from '@/lib/state-poller';
import { accountModeView } from '@/lib/account-modes';
import { DEPTH_NOTIONALS, depthQuoteView, type DepthQuote } from '@/lib/depth';
import {
  marginLimitFromPercent,
  migrationMarginLimit,
  migrationToleranceFromPercent,
  parseMinimumLeverage,
  percentFromMarginLimit,
  SUPPORTED_LEVERAGES,
} from '@/lib/policy';
import {
  migrationStatus,
  type MigrationConfig,
  type MigrationDraft,
  type MigrationState,
} from '@/lib/migration';

type Position = {
  symbol: string;
  side: string;
  qty: string;
  entry: string;
  mark: string;
  leverage: number;
  notional: string;
  occupied_margin: string;
  unrealized: string;
  liquidation: string;
};
type Policy = {
  threshold: string;
  order_notional: string;
  margin_limit: string;
  min_open_leverage?: number;
  spread_limit: string;
  symbols: string[];
};
type PolicyDraft = {
  threshold: string;
  order_notional: string;
  margin_percent: string;
  min_open_leverage: string;
};
type Account = {
  id: string;
  name: string;
  mode: string;
  env_prefix: string;
  enabled: boolean;
  status: string;
  reason: string;
  credential_ready: boolean;
  policy: Policy;
  migration?: MigrationConfig;
  migration_state?: MigrationState;
  risk_limits?: { base: string; high_leverage: string; migration?: string };
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
type Market = {
  status: string;
  error?: string;
  checked_at: number;
  capacities: Record<string, string>;
  book?: { bid: string; ask: string; mark: string; spread: string };
  depth?: DepthQuote;
  depth_error?: string;
};
type Event = {
  id: number;
  account_id: string;
  kind: string;
  message: string;
  created_at: number;
};
type State = {
  demo: boolean;
  ready: boolean;
  accounts: Account[];
  markets: Record<string, Market>;
  events: Event[];
  updated_at: number;
  notification: { configured: boolean; pending: number; error?: string };
};
const symbols = ['XAUUSD1', 'SPCXUSD1', 'CLUSD1'];
const names: Record<string, string> = {
  XAUUSD1: '黄金',
  SPCXUSD1: 'SpaceX',
  CLUSD1: '原油',
};
const fmt = (v?: string | number | null, digits = 2) =>
  v == null
    ? '—'
    : Number(v).toLocaleString('en-US', {
        maximumFractionDigits: digits,
        minimumFractionDigits: digits,
      });
const pct = (v?: string | number | null) =>
  v == null ? '—' : `${fmt(Number(v) * 100)}%`;
const clock = (v?: number) =>
  v ? new Date(v * 1000).toLocaleTimeString('zh-CN', { hour12: false }) : '—';

export default function Home() {
  const [state, setState] = useState<State | null>(null);
  const [selected, setSelected] = useState('');
  const [focus, setFocus] = useState('XAUUSD1');
  const [error, setError] = useState('');
  const [connectionError, setConnectionError] = useState('');
  const [needsLogin, setNeedsLogin] = useState(false);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [addOpen, setAddOpen] = useState(false);
  const [newAccount, setNewAccount] = useState({
    id: '',
    name: '',
    env_prefix: '',
    mode: 'live',
  });
  const [drafts, setDrafts] = useState<Record<string, PolicyDraft>>({});
  const [migrationDrafts, setMigrationDrafts] = useState<
    Record<string, MigrationDraft>
  >({});
  const [now, setNow] = useState(0);
  const [notice, setNotice] = useState('');
  const operationPending = useRef(false);
  const clearSession = useCallback(() => {
    setNeedsLogin(true);
    setState(null);
    setSelected('');
    setDrafts({});
    setMigrationDrafts({});
    setAddOpen(false);
    setConnectionError('');
  }, []);
  const [poller] = useState(() =>
    createStatePoller<State>({
      onState: (next) => {
        setState(next);
        setNeedsLogin(false);
        setConnectionError('');
        setSelected((v) =>
          next.accounts.some((a) => a.id === v)
            ? v
            : next.accounts[0]?.id || '',
        );
      },
      onUnauthorized: clearSession,
      onError: setConnectionError,
    }),
  );
  const refresh = useCallback(() => poller.refresh(), [poller]);
  useEffect(() => {
    poller.resume();
    const tick = () => {
      setNow(Date.now() / 1000);
      void refresh();
    };
    const initial = setTimeout(tick, 0);
    const timer = setInterval(tick, 3000);
    return () => {
      clearTimeout(initial);
      clearInterval(timer);
      poller.pause();
    };
  }, [refresh, poller]);
  const account = state?.accounts.find((a) => a.id === selected);
  const marginLimit = Number(account?.policy.margin_limit ?? '0.5');
  const marginPercent = percentFromMarginLimit(
    account?.policy.margin_limit ?? '0.5',
  );
  const highMarginLimitValue =
    account?.risk_limits?.high_leverage ??
    account?.policy.margin_limit ??
    '0.5';
  const highMarginLimit = Number(highMarginLimitValue);
  const highMarginPercent = percentFromMarginLimit(highMarginLimitValue);
  const migrationMarginPercent = percentFromMarginLimit(
    migrationMarginLimit(
      account?.policy.margin_limit ?? '0.5',
      account?.risk_limits,
    ),
  );
  const minimumLeverage = account?.policy.min_open_leverage ?? 5;
  const minimumLeverageSupported =
    SUPPORTED_LEVERAGES.includes(minimumLeverage);
  const form = drafts[selected] || {
    threshold: account?.policy.threshold || '10000',
    order_notional: account?.policy.order_notional || '1000',
    margin_percent: marginPercent,
    min_open_leverage: minimumLeverageSupported ? String(minimumLeverage) : '',
  };
  const setForm = (value: typeof form) =>
    setDrafts((previous) => ({ ...previous, [selected]: value }));
  const migrationForm = migrationDrafts[selected] || {
    enabled: account?.migration?.enabled ?? false,
    spread_limit_bp: account?.migration?.spread_limit_bp ?? '5',
    batch_notional: account?.migration?.batch_notional ?? '1000',
    tolerance_percent: percentFromMarginLimit(
      account?.migration?.notional_tolerance ?? '0.05',
    ),
  };
  const setMigrationForm = (value: MigrationDraft) =>
    setMigrationDrafts((previous) => ({ ...previous, [selected]: value }));
  const migration = account?.migration_state;
  const migrationView = migrationStatus(
    migration,
    account?.migration?.enabled ?? false,
    account?.enabled ?? false,
  );
  const action = async (
    url: string,
    body?: object,
    method: 'POST' | 'PATCH' = 'POST',
  ) => {
    if (operationPending.current) return false;
    operationPending.current = true;
    poller.pause();
    let resumePolling = true;
    setBusy(true);
    setError('');
    setNotice('');
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const r = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body || {}),
        signal: controller.signal,
      });
      const data = (await r.json().catch(() => ({}))) as { detail?: string };
      if (controller.signal.aborted) throw new Error('操作响应超时');
      clearTimeout(timeout);
      if (r.status === 401) {
        clearSession();
        resumePolling = false;
      }
      if (!r.ok) throw new Error(data.detail || '操作未完成');
      if (url === '/api/logout') {
        clearSession();
        resumePolling = false;
      } else {
        poller.resume();
        resumePolling = false;
        await refresh();
      }
      return true;
    } catch (e) {
      setError(
        controller.signal.aborted
          ? '操作结果暂未确认，请核对最新状态后再操作'
          : e instanceof Error
            ? e.message
            : '操作失败',
      );
      return false;
    } finally {
      clearTimeout(timeout);
      operationPending.current = false;
      if (resumePolling) {
        poller.resume();
        void refresh();
      }
      setBusy(false);
    }
  };
  const snapshot = account?.snapshot;
  const fresh = snapshot && now - snapshot.timestamp < 8 && !connectionError;
  const modeView = accountModeView(snapshot, now, connectionError);
  const ratio = snapshot ? Number(snapshot.ratio ?? 1) : 0;
  const market = state?.markets[focus];
  const strategy = account?.strategies[focus];
  const currentLeverage =
    snapshot?.positions.find((p) => p.symbol === focus)?.leverage ||
    minimumLeverage;
  const currentLeverageSupported =
    SUPPORTED_LEVERAGES.includes(currentLeverage);
  const highLeverage = currentLeverage === 10 || currentLeverage === 20;
  const openingLimit = highLeverage ? highMarginLimit : marginLimit;
  const openingPercent = highLeverage ? highMarginPercent : marginPercent;
  const riskAccent =
    ratio > highMarginLimit ? 'danger' : ratio > marginLimit ? 'amber' : 'mint';
  const positions =
    snapshot?.positions.filter((p) => Number(p.qty) !== 0) || [];
  const events =
    state?.events.filter(
      (e) => !selected || e.account_id === selected || !e.account_id,
    ) || [];
  // Paused accounts poll slowly. The enable endpoint re-reads and validates the
  // account, so an old display snapshot must not prevent requesting a restart.
  const canStart =
    account && !account.enabled && state?.ready && !connectionError;

  return (
    <main className="desk">
      <header className="topbar">
        <div className="brand">
          <Layers3 size={27} />
          <span>
            ASTER<span className="brand-separator">/</span>
            <small>交易管理</small>
          </span>
        </div>
        <div className="toolbar">
          <span className={`connection ${connectionError ? 'danger' : ''}`}>
            <i />
            {state?.demo ? '模拟环境' : state ? '服务已连接' : '等待连接'}
          </span>
          <Button
            variant="ghost"
            size="icon"
            aria-label="刷新数据"
            disabled={busy}
            onClick={() => {
              if (!operationPending.current)
                void poller.refresh({ resume: true });
            }}
          >
            <RefreshCw size={17} />
          </Button>
          {state && !state.demo && (
            <Button
              variant="ghost"
              size="icon"
              aria-label="退出登录"
              disabled={busy}
              onClick={() => void action('/api/logout')}
            >
              <LogOut size={17} />
            </Button>
          )}
        </div>
      </header>
      <div className="workspace">
        <div className="page-heading">
          <div>
            <div className="eyebrow">ACCOUNT OVERVIEW</div>
            <h1>账户与执行</h1>
          </div>
          <div className="account-controls">
            {account && (
              <Select
                value={selected}
                onValueChange={(v) => v && setSelected(v)}
              >
                <SelectTrigger className="account-select" aria-label="选择账户">
                  <Wallet size={16} />
                  <SelectValue>{account.name}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {state?.accounts.map((a) => (
                    <SelectItem key={a.id} value={a.id}>
                      {a.name} · {a.mode === 'live' ? '实盘' : '模拟'}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
            <Dialog open={addOpen} onOpenChange={setAddOpen}>
              <DialogTrigger
                render={<Button variant="outline" disabled={!state} />}
              >
                <Plus size={16} />
                添加账户
              </DialogTrigger>
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>添加子账户</DialogTitle>
                </DialogHeader>
                <form
                  className="form-stack"
                  onSubmit={async (e) => {
                    e.preventDefault();
                    if (await action('/api/accounts', newAccount)) {
                      setAddOpen(false);
                      setSelected(newAccount.id);
                      setNewAccount({
                        id: '',
                        name: '',
                        env_prefix: '',
                        mode: 'live',
                      });
                    }
                  }}
                >
                  <label htmlFor="account-name">
                    账户名称
                    <Input
                      id="account-name"
                      required
                      maxLength={50}
                      value={newAccount.name}
                      onChange={(e) =>
                        setNewAccount({ ...newAccount, name: e.target.value })
                      }
                      placeholder="主策略子账户"
                    />
                  </label>
                  <label htmlFor="account-id">
                    账户标识
                    <Input
                      id="account-id"
                      required
                      pattern="[a-z0-9_-]{1,32}"
                      value={newAccount.id}
                      onChange={(e) =>
                        setNewAccount({ ...newAccount, id: e.target.value })
                      }
                      placeholder="sub01"
                    />
                  </label>
                  <label htmlFor="account-prefix">
                    凭据环境变量前缀
                    <Input
                      id="account-prefix"
                      required
                      pattern="[A-Z][A-Z0-9_]{1,40}"
                      value={newAccount.env_prefix}
                      onChange={(e) =>
                        setNewAccount({
                          ...newAccount,
                          env_prefix: e.target.value.toUpperCase(),
                        })
                      }
                      placeholder="ASTER_SUB01"
                    />
                  </label>
                  <p className="muted">
                    只填写变量前缀。API 签名密钥由服务器读取。
                  </p>
                  <Select
                    value={newAccount.mode}
                    onValueChange={(v) =>
                      v && setNewAccount({ ...newAccount, mode: v })
                    }
                  >
                    <SelectTrigger aria-label="执行环境">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="live">实盘账户</SelectItem>
                      <SelectItem value="paper">模拟账户</SelectItem>
                    </SelectContent>
                  </Select>
                  <Button disabled={busy} type="submit">
                    保存账户，保持暂停
                  </Button>
                </form>
              </DialogContent>
            </Dialog>
          </div>
        </div>
        {(error || connectionError || notice) && (
          <output
            className={`message ${error || connectionError ? 'message-error' : ''}`}
          >
            {error || connectionError || notice}
          </output>
        )}
        {account && ['error', 'attention'].includes(account.status) && (
          <output className="message message-error">{account.reason}</output>
        )}
        {state?.demo && (
          <div className="demo-banner">
            模拟数据与模拟成交 · 不会向 Aster 发送订单
          </div>
        )}
        {needsLogin && (
          <section className="login-panel panel">
            <ShieldCheck size={27} />
            <h2>登录交易管理</h2>
            <p className="muted">输入服务器配置的访问密码。</p>
            <form
              onSubmit={async (e) => {
                e.preventDefault();
                if (await action('/api/login', { password })) setPassword('');
              }}
            >
              <Input
                aria-label="访问密码"
                autoComplete="current-password"
                type="password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              <Button disabled={busy} type="submit">
                登录
              </Button>
            </form>
          </section>
        )}
        {!needsLogin && (
          <>
            <section className="metrics">
              <Metric
                label="USD1 账户总权益"
                value={fmt(snapshot?.equity)}
                sub="全仓钱包余额 + 全仓未实现盈亏"
                icon={<Wallet size={17} />}
              />
              <Metric
                label="总名义持仓金额"
                value={fmt(snapshot?.total_notional)}
                sub="USD1 · 全部仓位按标记价格计，多空累加"
                icon={<Layers3 size={17} />}
              />
              <Metric
                label="账户保证金比率"
                value={pct(snapshot?.margin_ratio)}
                sub="维持保证金 ÷ 账户总权益"
                icon={<ShieldCheck size={17} />}
              />
              <Metric
                label="保证金占用率"
                value={snapshot ? pct(snapshot.ratio) : '—'}
                sub="全部仓位占用保证金 ÷ 账户总权益"
                accent={riskAccent}
                icon={<ShieldCheck size={17} />}
              />
              <Metric
                label="可用余额"
                value={fmt(snapshot?.available)}
                sub="USD1 · 用于下一笔开仓"
                icon={<Activity size={17} />}
              />
              <Metric
                label="未实现盈亏"
                value={fmt(snapshot?.unrealized)}
                sub={`${positions.length} 个方向持仓`}
                accent={Number(snapshot?.unrealized) < 0 ? 'danger' : 'mint'}
                icon={<ArrowUpRight size={17} />}
              />
            </section>
            <div className="main-grid">
              <div className="primary-column">
                <section className="panel">
                  <div className="section-head">
                    <div>
                      <h2>市场额度</h2>
                      <p>公开剩余额度 · USD1</p>
                    </div>
                    <span className="small-note">
                      额度 &gt; {fmt(account?.policy.threshold || 10000, 0)}{' '}
                      才触发
                    </span>
                  </div>
                  <section
                    className="table-scroll"
                    aria-label="市场额度与深度价差，可横向滚动"
                  >
                    <Table className="market-table">
                      <TableHeader>
                        <TableRow>
                          <TableHead>市场</TableHead>
                          {SUPPORTED_LEVERAGES.map((v) => (
                            <TableHead key={v} className="number">
                              {v}x
                            </TableHead>
                          ))}
                          <TableHead className="number">当前杠杆</TableHead>
                          {DEPTH_NOTIONALS.map((amount) => (
                            <TableHead key={amount} className="number">
                              {amount / 10000} 万 USD1
                              <span className="depth-heading">深度价差</span>
                            </TableHead>
                          ))}
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {symbols.map((s) => {
                          const m = state?.markets[s];
                          const live =
                            m?.status === 'ok' && now - m.checked_at < 20;
                          const lev = snapshot?.positions.find(
                            (p) => p.symbol === s,
                          )?.leverage;
                          return (
                            <TableRow
                              key={s}
                              className={focus === s ? 'selected-row' : ''}
                            >
                              <TableCell>
                                <button
                                  className="market-name"
                                  onClick={() => setFocus(s)}
                                >
                                  <strong>{s}</strong>
                                  <span>
                                    {names[s]} {!live && '· 等待数据'}
                                  </span>
                                </button>
                              </TableCell>
                              {SUPPORTED_LEVERAGES.map((v) => (
                                <TableCell
                                  key={v}
                                  className={`number ${live && Number(m.capacities[v]) > Number(account?.policy.threshold || 10000) ? 'mint' : ''}`}
                                >
                                  <span className="mobile-cell-label">
                                    {v}x 额度
                                  </span>
                                  {live ? fmt(m.capacities[v], 0) : '—'}
                                </TableCell>
                              ))}
                              <TableCell className="number">
                                <span className="mobile-cell-label">
                                  当前杠杆
                                </span>
                                {lev ? (
                                  <span className="leverage-chip">{lev}x</span>
                                ) : (
                                  '—'
                                )}
                              </TableCell>
                              {DEPTH_NOTIONALS.map((amount) => {
                                const quote = depthQuoteView(
                                  m?.depth,
                                  amount,
                                  now,
                                  connectionError || m?.depth_error,
                                );
                                return (
                                  <TableCell key={amount} className="number">
                                    <span className="mobile-cell-label">
                                      {amount / 10000} 万 USD1 深度价差
                                    </span>
                                    <div
                                      className={`depth-quote ${quote.stale ? 'depth-stale' : ''}`}
                                      title={quote.title}
                                    >
                                      <span>{quote.value}</span>
                                      <small>{quote.detail}</small>
                                    </div>
                                  </TableCell>
                                );
                              })}
                            </TableRow>
                          );
                        })}
                      </TableBody>
                    </Table>
                  </section>
                  <div className="table-footer">
                    <span>
                      <i className="dot mint-bg" />
                      按名义金额比较，多空不抵消
                    </span>
                    <span>1 bp = 万分之一</span>
                  </div>
                  <p className="depth-method">
                    深度价差按买入、卖出每边各 1 万 / 5 万 USD1
                    的成交均价计算，不含手续费。 每 10 秒采样，超过 15
                    秒标记过期；买卖盘各最多 1,000 档，1 bp = 万分之一。
                  </p>
                </section>
                <section className="panel activity-panel">
                  <Tabs defaultValue="positions">
                    <div className="section-head">
                      <TabsList variant="line">
                        <TabsTrigger value="positions">
                          当前持仓{' '}
                          <span className="count">{positions.length}</span>
                        </TabsTrigger>
                        <TabsTrigger value="events">执行记录</TabsTrigger>
                      </TabsList>
                      <span
                        className={`small-note ${snapshot && !fresh ? 'danger' : ''}`}
                      >
                        {snapshot
                          ? `${fresh ? '已同步' : '数据过期'} ${clock(snapshot.timestamp)}`
                          : '尚未接入账户'}
                      </span>
                    </div>
                    <TabsContent value="positions">
                      <div className="table-scroll">
                        <Table>
                          <TableHeader>
                            <TableRow>
                              <TableHead>市场 / 方向</TableHead>
                              <TableHead className="number">数量</TableHead>
                              <TableHead className="number">开仓均价</TableHead>
                              <TableHead className="number">标记价格</TableHead>
                              <TableHead className="number">杠杆</TableHead>
                              <TableHead className="number">名义价值</TableHead>
                              <TableHead className="number">
                                占用保证金
                              </TableHead>
                              <TableHead className="number">
                                预估强平价
                              </TableHead>
                              <TableHead className="number">
                                未实现盈亏
                              </TableHead>
                            </TableRow>
                          </TableHeader>
                          <TableBody>
                            {positions.map((p) => (
                              <TableRow key={`${p.symbol}-${p.side}`}>
                                <TableCell>
                                  <strong>{p.symbol}</strong>
                                  <span
                                    className={`direction ${p.side === 'LONG' ? 'mint' : 'danger'}`}
                                  >
                                    {p.side === 'LONG' ? (
                                      <ArrowUpRight size={14} />
                                    ) : (
                                      <ArrowDownLeft size={14} />
                                    )}{' '}
                                    {p.side === 'LONG' ? '多仓' : '空仓'}
                                  </span>
                                </TableCell>
                                <TableCell className="number">
                                  {fmt(p.qty, 4)}
                                </TableCell>
                                <TableCell className="number">
                                  {fmt(p.entry)}
                                </TableCell>
                                <TableCell className="number">
                                  {fmt(p.mark)}
                                </TableCell>
                                <TableCell className="number">
                                  {p.leverage}x
                                </TableCell>
                                <TableCell className="number">
                                  {fmt(p.notional)}
                                </TableCell>
                                <TableCell className="number">
                                  {fmt(p.occupied_margin)}
                                </TableCell>
                                <TableCell className="number">
                                  {Number(p.liquidation) > 0
                                    ? fmt(p.liquidation)
                                    : '—'}
                                </TableCell>
                                <TableCell
                                  className={`number ${Number(p.unrealized) < 0 ? 'danger' : 'mint'}`}
                                >
                                  {fmt(p.unrealized)}
                                </TableCell>
                              </TableRow>
                            ))}
                          </TableBody>
                        </Table>
                      </div>
                      {!positions.length && (
                        <div className="empty-state">
                          <Layers3 size={27} />
                          <h3>{account ? '暂无持仓' : '尚未添加子账户'}</h3>
                          <p>
                            {account
                              ? '满足额度、价差与风险条件后，策略才会分批开仓。'
                              : '添加账户后查看双向持仓、当前杠杆与风险。'}
                          </p>
                        </div>
                      )}
                    </TabsContent>
                    <TabsContent value="events">
                      <div className="event-list">
                        {events.length ? (
                          events.slice(0, 50).map((e) => (
                            <div className="event" key={e.id}>
                              <time>{clock(e.created_at)}</time>
                              <i
                                className={`event-dot ${e.kind === 'error' ? 'danger-bg' : e.kind === 'wait' ? 'amber-bg' : 'mint-bg'}`}
                              />
                              <p>{e.message}</p>
                            </div>
                          ))
                        ) : (
                          <div className="empty-state">
                            <Activity size={27} />
                            <h3>暂无执行记录</h3>
                            <p>开仓、杠杆调整与异常处理都会显示在这里。</p>
                          </div>
                        )}
                      </div>
                    </TabsContent>
                  </Tabs>
                </section>
              </div>
              <aside className="secondary-column">
                <section className="panel risk-panel">
                  <div className="section-head">
                    <h2>风险约束</h2>
                    <ShieldCheck className="mint" size={18} />
                  </div>
                  <div className="risk-value">
                    <span className={riskAccent}>
                      {snapshot ? pct(snapshot.ratio) : '—'}
                    </span>
                    <small>普通 5x ≤ {marginPercent}%</small>
                  </div>
                  <div className="risk-track">
                    <Progress
                      value={Math.min(100, ratio * 100)}
                      aria-label="当前保证金占用率"
                    />
                    <i
                      className="limit-marker"
                      style={{ left: `${marginPercent}%` }}
                      title={`普通 5x 基础上限 ${marginPercent}%`}
                    />
                    {highMarginLimit > marginLimit && (
                      <i
                        className="limit-marker high-limit-marker"
                        style={{ left: `${highMarginPercent}%` }}
                        title={`普通 10x / 20x 上限 ${highMarginPercent}%`}
                      />
                    )}
                  </div>
                  <div className="scale">
                    <span>0%</span>
                    <span>50%</span>
                    <span>100%</span>
                  </div>
                  <div className="risk-explanation muted">
                    <p className="risk-bonus">
                      <span>普通 10x / 20x 加仓上限</span>
                      <strong>{highMarginPercent}%</strong>
                    </p>
                    <p className="risk-bonus">
                      <span>迁移上限 · 含 5x</span>
                      <strong>{migrationMarginPercent}%</strong>
                    </p>
                    <p>共用额外 5 个百分点，最高 100%。</p>
                    <p>
                      每仓占用 = |数量| × 标记价格 ÷ 实际杠杆；多空分别累加。
                    </p>
                  </div>
                  <dl className="details">
                    <div>
                      <dt>总占用保证金 · USD1</dt>
                      <dd>{fmt(snapshot?.occupied_margin)}</dd>
                    </div>
                    {modeView.rows.map((mode) => (
                      <div key={mode.key}>
                        <dt>{mode.label}</dt>
                        <dd className={mode.failed ? 'danger' : ''}>
                          {mode.value}
                        </dd>
                      </div>
                    ))}
                  </dl>
                  <div className="mode-check-note">
                    <p title="模式结果来自这次账户快照">
                      {modeView.recordedAt
                        ? `账户快照 ${modeView.recordedAt}${modeView.elapsed ? ` · ${modeView.elapsed}` : ''}`
                        : modeView.hasSnapshot
                          ? '账户快照时间未知'
                          : '等待首次账户核验'}
                    </p>
                    {modeView.notice && (
                      <p className="amber">{modeView.notice}</p>
                    )}
                  </div>
                  <div className="risk-caption">
                    三项账户模式必须满足，仅核验，不自动修改。下单前与成交后均检查风险。
                  </div>
                </section>
                <section className="panel execution-panel">
                  <div className="section-head">
                    <h2>执行条件</h2>
                    <span className="symbol-tag">{focus}</span>
                  </div>
                  <Gate
                    label={
                      !minimumLeverageSupported
                        ? '请先选择受支持的最低开仓杠杆'
                        : !currentLeverageSupported
                          ? `当前 ${currentLeverage}x 不支持新增开仓`
                          : currentLeverage < minimumLeverage
                            ? `当前 ${currentLeverage}x，需先升至至少 ${minimumLeverage}x`
                            : `${currentLeverage}x 额度超过阈值`
                    }
                    pass={
                      currentLeverageSupported &&
                      minimumLeverageSupported &&
                      currentLeverage >= minimumLeverage &&
                      market?.status === 'ok' &&
                      Number(market.capacities[currentLeverage]) >
                        Number(account?.policy.threshold || 10000)
                    }
                    value={
                      currentLeverageSupported && market?.status === 'ok'
                        ? fmt(market.capacities[currentLeverage], 0)
                        : '—'
                    }
                  />
                  <Gate
                    label="BBO 价差 ≤ 万 5"
                    pass={
                      !!market?.book && Number(market.book.spread) <= 0.0005
                    }
                    value={
                      market?.book
                        ? `${fmt(Number(market.book.spread) * 10000)} bp`
                        : '—'
                    }
                  />
                  <Gate
                    label={`普通 ${currentLeverage}x 加仓占用率 < ${openingPercent}%`}
                    pass={!!fresh && ratio < openingLimit}
                    value={snapshot ? pct(snapshot.ratio) : '—'}
                  />
                  <div className="strategy-state">
                    <i
                      className={`dot ${account?.enabled ? 'mint-bg' : 'amber-bg'}`}
                    />
                    <span>
                      {strategy?.reason || account?.reason || '等待接入账户'}
                    </span>
                  </div>
                  <div className="execution-buttons">
                    <Button
                      disabled={busy || !canStart}
                      title="启动前会重新核对账户，满足条件后恢复策略"
                      onClick={() =>
                        account &&
                        void action(`/api/accounts/${account.id}/enable`)
                      }
                    >
                      <CirclePlay size={16} />
                      {account?.mode === 'paper' ? '启动模拟' : '启动实盘'}
                    </Button>
                    <Button
                      variant="outline"
                      disabled={busy || !account?.enabled}
                      onClick={() =>
                        account &&
                        void action(`/api/accounts/${account.id}/pause`)
                      }
                    >
                      <CirclePause size={16} />
                      暂停策略
                    </Button>
                  </div>
                  {account?.status === 'attention' && (
                    <Button
                      className="reconcile-button"
                      variant="outline"
                      disabled={busy}
                      onClick={() =>
                        void action(`/api/accounts/${account.id}/retry`)
                      }
                    >
                      核对未完成批次
                    </Button>
                  )}
                </section>
                <section className="panel settings-panel migration-panel">
                  <div className="section-head">
                    <h2>XAU 仓位迁移</h2>
                    <span
                      className={`migration-badge ${migrationView.phase === 'attention' || migrationView.phase === 'residual' ? 'amber' : ''}`}
                    >
                      {migrationView.label}
                    </span>
                  </div>
                  <div className="migration-state" aria-live="polite">
                    {connectionError ? (
                      <p className="amber">连接中断，以下为最近记录</p>
                    ) : null}
                    <p>{migrationView.reason}</p>
                    {migration?.active_batch && !account?.enabled ? (
                      <p className="amber">
                        已停止开始新批次，正在核对当前批次。
                      </p>
                    ) : null}
                  </div>
                  {account?.migration?.enabled ? (
                    <Button
                      variant="outline"
                      className="reconcile-button"
                      disabled={busy}
                      onClick={async () => {
                        if (
                          await action(
                            `/api/accounts/${account.id}`,
                            { migration: { enabled: false } },
                            'PATCH',
                          )
                        ) {
                          setMigrationDrafts((previous) => {
                            const next = { ...previous };
                            delete next[account.id];
                            return next;
                          });
                          setNotice('迁移已停止，账户保持暂停');
                        }
                      }}
                    >
                      停止迁移并暂停账户
                    </Button>
                  ) : null}
                  <dl className="migration-details">
                    <div>
                      <dt>XAU 剩余多头数量</dt>
                      <dd>
                        {migration?.source_remaining_qty?.LONG ??
                          snapshot?.positions.find(
                            (p) => p.symbol === 'XAUUSD1' && p.side === 'LONG',
                          )?.qty ??
                          '—'}
                      </dd>
                    </div>
                    <div>
                      <dt>XAU 剩余空头数量</dt>
                      <dd>
                        {migration?.source_remaining_qty?.SHORT ??
                          snapshot?.positions.find(
                            (p) => p.symbol === 'XAUUSD1' && p.side === 'SHORT',
                          )?.qty ??
                          '—'}
                      </dd>
                    </div>
                    <div>
                      <dt>当前目标</dt>
                      <dd>
                        {migration?.active_batch?.target_symbol ||
                          migration?.target_symbol ||
                          '—'}
                      </dd>
                    </div>
                    <div>
                      <dt>目标 / 要求杠杆</dt>
                      <dd>
                        {migration?.target_leverage
                          ? `${migration.target_leverage}x`
                          : '—'}{' '}
                        /{' '}
                        {migration?.required_leverage
                          ? `≥ ${migration.required_leverage}x`
                          : '—'}
                      </dd>
                    </div>
                    <div>
                      <dt>临时迁移占用上限</dt>
                      <dd>{migrationMarginPercent}%</dd>
                    </div>
                    <div>
                      <dt>适用迁移杠杆</dt>
                      <dd>5x / 10x / 20x</dd>
                    </div>
                    <div>
                      <dt>多头累计已迁 · USD1</dt>
                      <dd>{fmt(migration?.migrated_notional?.LONG)}</dd>
                    </div>
                    <div>
                      <dt>空头累计已迁 · USD1</dt>
                      <dd>{fmt(migration?.migrated_notional?.SHORT)}</dd>
                    </div>
                    <div>
                      <dt>多头累计金额差 · USD1</dt>
                      <dd>{fmt(migration?.cumulative_notional_delta?.LONG)}</dd>
                    </div>
                    <div>
                      <dt>空头累计金额差 · USD1</dt>
                      <dd>
                        {fmt(migration?.cumulative_notional_delta?.SHORT)}
                      </dd>
                    </div>
                  </dl>
                  <p className="migration-footnote">
                    已完成 {migration?.completed_batches ?? 0} 批 ·
                    金额差为目标开仓减去 XAU 平仓，按多空分别累计。
                  </p>
                  <form
                    onSubmit={async (e) => {
                      e.preventDefault();
                      if (!account) return;
                      try {
                        const migrationSettings = {
                          enabled: migrationForm.enabled,
                          spread_limit_bp: migrationForm.spread_limit_bp,
                          batch_notional: migrationForm.batch_notional,
                          notional_tolerance: migrationToleranceFromPercent(
                            migrationForm.tolerance_percent,
                          ),
                        };
                        if (
                          await action(
                            `/api/accounts/${account.id}`,
                            { migration: migrationSettings },
                            'PATCH',
                          )
                        ) {
                          setMigrationDrafts((previous) => {
                            const next = { ...previous };
                            delete next[account.id];
                            return next;
                          });
                          setNotice('迁移设置已保存，启动账户后按设置运行');
                        }
                      } catch (e) {
                        setNotice('');
                        setError(
                          e instanceof Error ? e.message : '迁移设置无效',
                        );
                      }
                    }}
                  >
                    <div className="migration-toggle">
                      <label htmlFor="migration-enabled">
                        允许迁移 XAU 仓位
                      </label>
                      <Switch
                        id="migration-enabled"
                        aria-label="允许迁移 XAU 仓位"
                        disabled={
                          busy ||
                          !account ||
                          account.enabled ||
                          !!migration?.active_batch
                        }
                        checked={migrationForm.enabled}
                        onCheckedChange={(enabled) =>
                          setMigrationForm({ ...migrationForm, enabled })
                        }
                      />
                    </div>
                    <label htmlFor="migration-spread">
                      目标深度价差上限 <span>bp</span>
                      <Input
                        id="migration-spread"
                        type="number"
                        min="0"
                        max="100"
                        step="any"
                        required
                        disabled={
                          busy ||
                          !account ||
                          account.enabled ||
                          !!migration?.active_batch
                        }
                        value={migrationForm.spread_limit_bp}
                        onChange={(e) =>
                          setMigrationForm({
                            ...migrationForm,
                            spread_limit_bp: e.target.value,
                          })
                        }
                      />
                    </label>
                    <label htmlFor="migration-batch">
                      单批每边上限 <span>USD1</span>
                      <Input
                        id="migration-batch"
                        type="number"
                        min="500"
                        max="1000000"
                        step="any"
                        required
                        disabled={
                          busy ||
                          !account ||
                          account.enabled ||
                          !!migration?.active_batch
                        }
                        value={migrationForm.batch_notional}
                        onChange={(e) =>
                          setMigrationForm({
                            ...migrationForm,
                            batch_notional: e.target.value,
                          })
                        }
                      />
                    </label>
                    <label htmlFor="migration-tolerance">
                      每边金额误差上限 <span>%</span>
                      <Input
                        id="migration-tolerance"
                        type="number"
                        min="0"
                        max="50"
                        step="any"
                        required
                        disabled={
                          busy ||
                          !account ||
                          account.enabled ||
                          !!migration?.active_batch
                        }
                        value={migrationForm.tolerance_percent}
                        onChange={(e) =>
                          setMigrationForm({
                            ...migrationForm,
                            tolerance_percent: e.target.value,
                          })
                        }
                      />
                    </label>
                    <p className="muted">
                      只在 SPCX / CL 有有效 5x
                      额度、实际杠杆不低于原仓位时迁移，优先选择本批深度价差较小的目标。先开目标多空并确认，再平
                      XAU。迁移临时上限为基础加 5 个百分点，最高
                      100%，计入未平 XAU 占用并预留四腿成本；保证金不足时等待。常规批次每边至少 500
                      USD1，最后尾批可按交易所最小下单规则收尾。
                    </p>
                    <p className="muted">
                      开关开启后，普通策略暂停新增仓位，迁移完成后也保持暂停。修改前请暂停账户并等待本批核对完成；保存不会启动账户。关闭再开启会建立新一轮迁移。
                    </p>
                    <Button
                      variant="outline"
                      className="full-width"
                      disabled={
                        busy ||
                        !account ||
                        account.enabled ||
                        !!migration?.active_batch
                      }
                      type="submit"
                    >
                      保存迁移设置
                    </Button>
                  </form>
                </section>
                <section className="panel settings-panel">
                  <div className="section-head">
                    <h2>策略设置</h2>
                    <SlidersHorizontal size={17} />
                  </div>
                  <form
                    onSubmit={async (e) => {
                      e.preventDefault();
                      if (!account) return;
                      try {
                        const policy = {
                          threshold: form.threshold,
                          order_notional: form.order_notional,
                          margin_limit: marginLimitFromPercent(
                            form.margin_percent,
                          ),
                          min_open_leverage: parseMinimumLeverage(
                            form.min_open_leverage,
                          ),
                        };
                        if (
                          await action(
                            `/api/accounts/${account.id}`,
                            policy,
                            'PATCH',
                          )
                        ) {
                          setDrafts((previous) => {
                            const next = { ...previous };
                            delete next[account.id];
                            return next;
                          });
                          setNotice('策略设置已保存');
                        }
                      } catch (e) {
                        setNotice('');
                        setError(e instanceof Error ? e.message : '设置无效');
                      }
                    }}
                  >
                    <label htmlFor="threshold">
                      额度阈值 <span>USD1</span>
                      <Input
                        id="threshold"
                        type="number"
                        min="0"
                        step="any"
                        required
                        disabled={busy || !account || account.enabled}
                        value={form.threshold}
                        onChange={(e) =>
                          setForm({ ...form, threshold: e.target.value })
                        }
                      />
                    </label>
                    <label htmlFor="order-notional">
                      单笔每边上限 <span>USD1</span>
                      <Input
                        id="order-notional"
                        type="number"
                        min="500"
                        max="1000000"
                        step="any"
                        required
                        disabled={busy || !account || account.enabled}
                        value={form.order_notional}
                        onChange={(e) =>
                          setForm({ ...form, order_notional: e.target.value })
                        }
                      />
                      <span>新开仓固定每边至少 500 USD1</span>
                    </label>
                    <label htmlFor="margin-percent">
                      基础风险上限 <span>%</span>
                      <Input
                        id="margin-percent"
                        type="number"
                        min="0"
                        max="100"
                        step="any"
                        required
                        disabled={busy || !account || account.enabled}
                        value={form.margin_percent}
                        onChange={(e) =>
                          setForm({ ...form, margin_percent: e.target.value })
                        }
                      />
                    </label>
                    <label htmlFor="min-open-leverage">
                      最低开仓杠杆 <span>x</span>
                      <Select
                        required
                        disabled={busy || !account || account.enabled}
                        value={form.min_open_leverage || null}
                        onValueChange={(value) =>
                          value &&
                          setForm({ ...form, min_open_leverage: value })
                        }
                      >
                        <SelectTrigger
                          id="min-open-leverage"
                          className="full-width"
                        >
                          <SelectValue placeholder="请选择最低开仓杠杆" />
                        </SelectTrigger>
                        <SelectContent>
                          {SUPPORTED_LEVERAGES.map((value) => (
                            <SelectItem key={value} value={String(value)}>
                              {value}x
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    </label>
                    <p className="muted">
                      修改前请暂停策略。基础风险约束大于 0%、不超过 100%。普通
                      5x 新增使用基础上限；普通 10x / 20x 和迁移各档（含
                      5x）共用额外 5 个百分点，最高
                      100%。实际下单量还受余额与盘口限制。最低杠杆可设 5x、10x
                      或 20x，调低该值不会降低已有杠杆。
                    </p>
                    <Button
                      variant="outline"
                      className="full-width"
                      disabled={busy || !account || account.enabled}
                      type="submit"
                    >
                      保存设置
                    </Button>
                  </form>
                </section>
                <div className="notification-state">
                  <Bell size={15} />
                  <span>
                    {state?.notification.error ||
                      (state?.notification.configured
                        ? `飞书已配置${state.notification.pending ? ` · ${state.notification.pending} 条待发送` : ' · 开仓完成后通知'}`
                        : '飞书待配置 · 在服务器设置通知凭据')}
                  </span>
                </div>
              </aside>
            </div>
            <footer className="footer">
              <span>ASTER ACCOUNT DESK</span>
              <span>
                {state
                  ? `${state.accounts.length} 个账户 · ${clock(state.updated_at)}`
                  : '交易服务尚未连接'}
              </span>
            </footer>
          </>
        )}
      </div>
    </main>
  );
}
function Metric({
  label,
  value,
  sub,
  icon,
  accent = '',
}: {
  label: string;
  value: string;
  sub: string;
  icon: React.ReactNode;
  accent?: string;
}) {
  return (
    <article className="metric">
      <div className="metric-label">
        {label}
        {icon}
      </div>
      <div className={`metric-value ${accent}`}>{value}</div>
      <p>{sub}</p>
    </article>
  );
}
function Gate({
  label,
  value,
  pass,
}: {
  label: string;
  value: string;
  pass: boolean;
}) {
  return (
    <div className="gate">
      <span>
        <i className={`dot ${pass ? 'mint-bg' : 'muted-bg'}`} />
        {label}
      </span>
      <strong>{value}</strong>
    </div>
  );
}
