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
import {
  marginLimitFromPercent,
  parseMinimumLeverage,
  percentFromMarginLimit,
} from '@/lib/policy';

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
  const [now, setNow] = useState(0);
  const [notice, setNotice] = useState('');
  const operationPending = useRef(false);
  const clearSession = useCallback(() => {
    setNeedsLogin(true);
    setState(null);
    setSelected('');
    setDrafts({});
    setAddOpen(false);
    setConnectionError('');
  }, []);
  const [poller] = useState(() => createStatePoller<State>({
    onState: (next) => {
      setState(next);
      setNeedsLogin(false);
      setConnectionError('');
      setSelected((v) =>
        next.accounts.some((a) => a.id === v) ? v : next.accounts[0]?.id || '',
      );
    },
    onUnauthorized: clearSession,
    onError: setConnectionError,
  }));
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
  const minimumLeverage = account?.policy.min_open_leverage ?? 4;
  const displayedTiers = [...new Set([4, 5, 10, 20, minimumLeverage])].sort(
    (a, b) => a - b,
  );
  const form = drafts[selected] || {
    threshold: account?.policy.threshold || '10000',
    order_notional: account?.policy.order_notional || '1000',
    margin_percent: marginPercent,
    min_open_leverage: String(minimumLeverage),
  };
  const setForm = (value: typeof form) =>
    setDrafts((previous) => ({ ...previous, [selected]: value }));
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
      setError(controller.signal.aborted
        ? '操作结果暂未确认，请核对最新状态后再操作'
        : e instanceof Error ? e.message : '操作失败');
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
  const ratio = snapshot ? Number(snapshot.ratio ?? 1) : 0;
  const market = state?.markets[focus];
  const strategy = account?.strategies[focus];
  const currentLeverage =
    snapshot?.positions.find((p) => p.symbol === focus)?.leverage ||
    minimumLeverage;
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
            onClick={() => void refresh()}
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
                accent={ratio > marginLimit ? 'danger' : 'mint'}
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
                  <div className="table-scroll">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>市场</TableHead>
                          {displayedTiers.map((v) => (
                            <TableHead key={v} className="number">
                              {v}x
                            </TableHead>
                          ))}
                          <TableHead className="number">当前杠杆</TableHead>
                          <TableHead className="number">BBO 价差</TableHead>
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
                              {displayedTiers.map((v) => (
                                <TableCell
                                  key={v}
                                  className={`number ${live && Number(m.capacities[v]) > Number(account?.policy.threshold || 10000) ? 'mint' : ''}`}
                                >
                                  {live ? fmt(m.capacities[v], 0) : '—'}
                                </TableCell>
                              ))}
                              <TableCell className="number">
                                {lev ? (
                                  <span className="leverage-chip">{lev}x</span>
                                ) : (
                                  '—'
                                )}
                                {lev && !displayedTiers.includes(lev) && (
                                  <small className="current-capacity">
                                    额度{' '}
                                    {live ? fmt(m?.capacities[lev], 0) : '—'}
                                  </small>
                                )}
                              </TableCell>
                              <TableCell
                                className={`number ${Number(m?.book?.spread) > 0.0005 ? 'danger' : ''}`}
                              >
                                {live && m.book
                                  ? `${fmt(Number(m.book.spread) * 10000)} bp`
                                  : '—'}
                              </TableCell>
                            </TableRow>
                          );
                        })}
                      </TableBody>
                    </Table>
                  </div>
                  <div className="table-footer">
                    <span>
                      <i className="dot mint-bg" />
                      按名义金额比较，多空不抵消
                    </span>
                    <span>1 bp = 万分之一</span>
                  </div>
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
                    <span className={ratio > marginLimit ? 'danger' : ''}>
                      {snapshot ? pct(snapshot.ratio) : '—'}
                    </span>
                    <small>加仓后 ≤ {marginPercent}%</small>
                  </div>
                  <div className="risk-track">
                    <Progress
                      value={Math.min(100, ratio * 100)}
                      aria-label="当前保证金占用率"
                    />
                    <i
                      className="limit-marker"
                      style={{ left: `${marginPercent}%` }}
                      title={`风险上限 ${marginPercent}%`}
                    />
                  </div>
                  <div className="scale">
                    <span>0%</span>
                    <span>50%</span>
                    <span>100%</span>
                  </div>
                  <p className="muted">
                    每仓占用 = |数量| × 标记价格 ÷ 实际杠杆；多空分别累加。
                  </p>
                  <dl className="details">
                    <div>
                      <dt>总占用保证金 · USD1</dt>
                      <dd>{fmt(snapshot?.occupied_margin)}</dd>
                    </div>
                    <div>
                      <dt>全仓保证金模式</dt>
                      <dd
                        className={
                          fresh && !snapshot?.mode_checks?.cross ? 'danger' : ''
                        }
                      >
                        {fresh
                          ? snapshot.mode_checks?.cross
                            ? '已核实'
                            : '不符合要求'
                          : '待核实'}
                      </dd>
                    </div>
                    <div>
                      <dt>双向持仓模式</dt>
                      <dd
                        className={
                          fresh && !snapshot?.mode_checks?.hedge ? 'danger' : ''
                        }
                      >
                        {fresh
                          ? snapshot.mode_checks?.hedge
                            ? '已核实'
                            : '不符合要求'
                          : '待核实'}
                      </dd>
                    </div>
                    <div>
                      <dt>单币保证金模式 · USD1</dt>
                      <dd
                        className={
                          fresh && !snapshot?.mode_checks?.single_asset
                            ? 'danger'
                            : ''
                        }
                      >
                        {fresh
                          ? snapshot.mode_checks?.single_asset
                            ? '已核实'
                            : '不符合要求'
                          : '待核实'}
                      </dd>
                    </div>
                  </dl>
                  <div className="risk-caption">
                    三项账户模式为固定前提，仅核验，不提供修改。每笔下单前检查预计成交后的风险，成交后再次核对。
                  </div>
                </section>
                <section className="panel execution-panel">
                  <div className="section-head">
                    <h2>执行条件</h2>
                    <span className="symbol-tag">{focus}</span>
                  </div>
                  <Gate
                    label={
                      currentLeverage < minimumLeverage
                        ? `当前 ${currentLeverage}x，需先升至至少 ${minimumLeverage}x`
                        : `${currentLeverage}x 额度超过阈值`
                    }
                    pass={
                      currentLeverage >= minimumLeverage &&
                      market?.status === 'ok' &&
                      Number(market.capacities[currentLeverage]) >
                        Number(account?.policy.threshold || 10000)
                    }
                    value={
                      market?.status === 'ok'
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
                    label={`保证金占用率 ≤ ${marginPercent}%`}
                    pass={!!fresh && ratio <= marginLimit}
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
                      风险约束上限 <span>%</span>
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
                      <Input
                        id="min-open-leverage"
                        type="number"
                        min="1"
                        max="125"
                        step="1"
                        required
                        disabled={busy || !account || account.enabled}
                        value={form.min_open_leverage}
                        onChange={(e) =>
                          setForm({
                            ...form,
                            min_open_leverage: e.target.value,
                          })
                        }
                      />
                    </label>
                    <p className="muted">
                      修改前请暂停策略。风险约束为保证金占用率上限，大于
                      0%、不超过
                      100%；实际下单量还受余额与盘口限制。最低杠杆可设
                      1–125，调低该值不会降低已有杠杆。
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
