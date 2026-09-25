'use client';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Bell,
  CirclePause,
  CirclePlay,
  Layers3,
  LogOut,
  RefreshCw,
  ShieldCheck,
  Wallet,
} from 'lucide-react';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useTradingDesk } from '@/lib/use-trading-desk';
import { useAccountDraft } from '@/lib/use-account-draft';
import { fmt, pct, clock } from '@/lib/desk-format';
import { cycleDraft, cycleRecoveryReason } from '@/lib/cycle';
import { accountRunScope } from '@/lib/account-scope';
import { accountSnapshotView } from '@/lib/account-modes';
import { cycleMarginLimit } from '@/lib/policy';
import { CyclePanel } from '@/components/cycle-panel';
import { CycleRecovery } from '@/components/cycle-recovery';
import { OrdinaryWorkspace } from '@/components/ordinary-workspace';
import { MigrationPanel } from '@/components/migration-panel';
import { AccountOverview } from '@/components/account-overview';
import { RecordsWorkspace } from '@/components/records-workspace';
import { AddAccountDialog } from '@/components/add-account-dialog';
import { ListingsPanel } from '@/components/listings-panel';
import { MonitoringPanel } from '@/components/monitoring-panel';

export default function Home() {
  const desk = useTradingDesk();
  // Session changes unmount the feature workspace and discard its private drafts.
  return (
    <Desk key={desk.needsLogin ? 'signed-out' : 'signed-in'} desk={desk} />
  );
}
function Desk({ desk }: { desk: ReturnType<typeof useTradingDesk> }) {
  const {
    state,
    selected,
    selectAccount,
    error,
    setError,
    errorAccountId,
    connectionError,
    needsLogin,
    password,
    setPassword,
    busy,
    serverNow,
    notice,
    setNotice,
    action,
    refresh,
  } = desk;
  const account = state?.accounts.find((account) => account.id === selected);
  const monitoringProps = {
    monitoring: state?.monitoring,
    notification: state?.notification,
    demo: Boolean(state?.demo),
    busy,
    connectionError,
    action,
  };
  const cycle = useAccountDraft(selected, cycleDraft(account?.cycle));
  const snapshot = account?.snapshot;
  const recoveryReason = cycleRecoveryReason(account, {
    accountId: errorAccountId,
    message: error,
  });
  const accountMessage =
    recoveryReason ||
    (account && ['error', 'attention'].includes(account.status)
      ? account.reason
      : '');
  const separateError = error === accountMessage ? '' : error;
  const canStart = Boolean(
    account &&
    !account.enabled &&
    state?.ready &&
    !connectionError &&
    !cycle.dirty,
  );
  const stale =
    Boolean(connectionError) ||
    !state?.updated_at ||
    serverNow - state.updated_at >= 8;
  const snapshotView = accountSnapshotView(
    snapshot,
    serverNow,
    connectionError,
    account?.snapshot_refresh,
    account?.status === 'error' ? account.reason : undefined,
  );
  const events =
    state?.events.filter(
      (event) =>
        !selected || event.account_id === selected || !event.account_id,
    ) ?? [];
  const featureProps = account
    ? { account, busy, action, setError, setNotice }
    : null;
  const modes = account ? accountRunScope(account) : '';
  const riskLimit = account?.cycle?.enabled
    ? cycleMarginLimit(account.risk_limits)
    : (account?.risk_limits?.high_leverage ?? account?.policy.margin_limit);
  const marginTone =
    snapshot?.ratio == null || riskLimit == null
      ? ''
      : Number(snapshot.ratio) > Number(riskLimit)
        ? 'danger'
        : Number(snapshot.ratio) > Number(account?.policy.margin_limit)
          ? 'amber'
          : '';
  return (
    <main className="desk focused-desk">
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
            {connectionError
              ? '连接异常'
              : state?.demo
                ? '模拟环境'
                : state
                  ? '服务已连接'
                  : '等待连接'}
          </span>
          <Button
            variant="ghost"
            size="icon"
            aria-label="刷新数据"
            disabled={busy}
            onClick={refresh}
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
          <div className="flex items-center gap-3">
            <h1>交易工作台</h1>
            {desk.hubConnected && (
              <Button variant="ghost" onClick={desk.openAssets}>
                查看资产账本
              </Button>
            )}
          </div>
          <div className="account-controls">
            {' '}
            {account && (
              <Select
                value={selected}
                disabled={busy}
                onValueChange={(v) => {
                  if (!v) return;
                  selectAccount(v);
                  setError('');
                  setNotice('');
                }}
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
            <AddAccountDialog
              busy={busy}
              action={action}
              selectAccount={selectAccount}
              available={Boolean(state)}
            />
          </div>
        </div>
        {(separateError || connectionError || notice) && (
          <output
            className={`message ${separateError || connectionError ? 'message-error' : ''}`}
          >
            {separateError || connectionError || notice}
          </output>
        )}
        {account && accountMessage && (
          <div className="message message-error cycle-recovery-message">
            <output>{accountMessage}</output>
            {recoveryReason ? (
              <CycleRecovery
                key={account.id}
                accountId={account.id}
                accountName={account.name}
                disabled={busy || Boolean(connectionError)}
                action={action}
                setNotice={setNotice}
              />
            ) : null}
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

        {!needsLogin && account && featureProps ? (
          <>
            <section className="account-bar" aria-label="账户状态与总开关">
              <div className="account-run-state">
                <span
                  className={`run-indicator ${account.enabled ? 'mint' : 'muted'}`}
                >
                  <i />
                  {account.enabled ? '账户运行中' : '账户已暂停'}
                </span>
                <p>启动范围：{modes}</p>
              </div>
              <div className="account-run-actions">
                <Button
                  disabled={busy || !canStart}
                  title={
                    cycle.dirty
                      ? '请先保存或撤销循环草稿'
                      : '按已保存配置启动账户的全部已启用功能'
                  }
                  onClick={() =>
                    void action(`/api/accounts/${account.id}/enable`)
                  }
                >
                  <CirclePlay size={16} />
                  启动账户
                </Button>
                <Button
                  variant="outline"
                  disabled={busy || !account.enabled}
                  onClick={() =>
                    void action(`/api/accounts/${account.id}/pause`)
                  }
                >
                  <CirclePause size={16} />
                  暂停账户
                </Button>
                {account.status === 'attention' ? (
                  <Button
                    variant="outline"
                    disabled={busy}
                    onClick={() =>
                      void action(`/api/accounts/${account.id}/retry`)
                    }
                  >
                    核对未完成批次
                  </Button>
                ) : null}
              </div>
              {cycle.dirty ? (
                <p className="draft-notice amber">
                  循环设置有未保存修改，请到「多空循环」保存或撤销。
                </p>
              ) : null}
            </section>
            <section className="account-strip" aria-label="账户关键数据">
              <dl>
                <div>
                  <dt>权益 · USD1</dt>
                  <dd>{fmt(snapshot?.equity)}</dd>
                </div>
                <div>
                  <dt>可用 · USD1</dt>
                  <dd>{fmt(snapshot?.available)}</dd>
                </div>
                <div>
                  <dt>保证金占用率</dt>
                  <dd className={marginTone}>{pct(snapshot?.ratio)}</dd>
                </div>
              </dl>
              <span
                className={`snapshot-stamp ${snapshotView.warning ? 'amber' : ''}`}
                title={snapshotView.notice}
              >
                {snapshot
                  ? `${snapshotView.label} ${clock(snapshot.timestamp)}${snapshotView.elapsed ? ` · ${snapshotView.elapsed}` : ''}`
                  : '等待账户数据'}
                {state?.demo ? ' · 模拟环境' : ''}
              </span>
            </section>
            <Tabs defaultValue="cycle" className="workspace-tabs">
              <TabsList
                variant="line"
                aria-label="工作台功能"
                className="workspace-navigation"
              >
                <TabsTrigger value="cycle">多空循环</TabsTrigger>
                <TabsTrigger value="ordinary">普通开仓</TabsTrigger>
                <TabsTrigger value="migration">仓位迁移</TabsTrigger>
                <TabsTrigger value="listings">USD1 上新</TabsTrigger>
                <TabsTrigger value="monitoring">监控与告警</TabsTrigger>
                <TabsTrigger value="account">账户</TabsTrigger>
                <TabsTrigger value="records">记录</TabsTrigger>
              </TabsList>
              <TabsContent value="cycle" keepMounted>
                <CyclePanel
                  {...featureProps}
                  draft={cycle.value}
                  setDraft={cycle.setValue}
                  clearDraft={cycle.clear}
                  now={serverNow}
                  dataTimestamp={state?.updated_at}
                  connectionError={connectionError}
                />
              </TabsContent>
              <TabsContent value="ordinary" keepMounted>
                <OrdinaryWorkspace
                  {...featureProps}
                  markets={state?.markets ?? {}}
                  now={serverNow}
                  connectionError={connectionError}
                />
              </TabsContent>
              <TabsContent value="migration" keepMounted>
                <MigrationPanel
                  {...featureProps}
                  connectionError={connectionError}
                />
              </TabsContent>
              <TabsContent value="account" keepMounted>
                <AccountOverview
                  {...featureProps}
                  now={serverNow}
                  connectionError={connectionError}
                />
              </TabsContent>
              <TabsContent value="listings">
                <ListingsPanel
                  action={action}
                  busy={busy}
                  listings={state?.listings}
                  monitoring={state?.monitoring}
                  notification={state?.notification}
                  now={serverNow}
                  connectionError={connectionError}
                />
              </TabsContent>
              <TabsContent value="monitoring">
                <MonitoringPanel {...monitoringProps} />
              </TabsContent>
              <TabsContent value="records">
                <RecordsWorkspace
                  account={account}
                  events={events}
                  now={serverNow}
                  stale={stale}
                />
              </TabsContent>
            </Tabs>
            <footer className="footer">
              <span>
                <Bell size={13} />{' '}
                {state?.notification.enabled === false
                  ? '飞书告警已关闭'
                  : state?.notification.error ||
                    (state?.notification.configured
                      ? `飞书已连接${state.notification.pending ? ` · ${state.notification.pending} 条待发送` : ''}`
                      : '飞书未配置')}
              </span>
              <span>
                {state?.accounts.length} 个账户 · 更新{' '}
                {clock(state?.updated_at)}
              </span>
            </footer>
          </>
        ) : !needsLogin ? (
          <>
            <Tabs defaultValue="listings" className="workspace-tabs">
              <TabsList
                variant="line"
                aria-label="监控功能"
                className="workspace-navigation"
              >
                <TabsTrigger value="listings">USD1 上新</TabsTrigger>
                <TabsTrigger value="monitoring">监控与告警</TabsTrigger>
              </TabsList>
              <TabsContent value="listings">
                <ListingsPanel
                  action={action}
                  busy={busy}
                  listings={state?.listings}
                  monitoring={state?.monitoring}
                  notification={state?.notification}
                  now={serverNow}
                  connectionError={connectionError}
                />
              </TabsContent>
              <TabsContent value="monitoring">
                <MonitoringPanel {...monitoringProps} />
              </TabsContent>
            </Tabs>
            <section className="panel empty-state">
              <h2>{state ? '添加账户以管理交易' : '正在连接交易服务'}</h2>
            </section>
          </>
        ) : null}
      </div>
    </main>
  );
}
