'use client';
import { useState } from 'react';
import type { DeskAction, Monitoring, State } from '@/lib/desk-types';
import { isDisplayTimestamp } from '@/lib/display-time';
import {
  notificationChannel,
  notificationOverview,
} from '@/lib/notification-channels';
import { Input } from '@/components/ui/input';
import { SummaryIntervalForm } from '@/components/summary-interval-form';
import { Switch } from '@/components/ui/switch';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';

const categories: {
  key:
    | 'new_listing_alerts'
    | 'strategy_capacity_alerts'
    | 'listing_capacity_alerts'
    | 'trade_summary_alerts';
  title: string;
  description: string;
}[] = [
  {
    key: 'new_listing_alerts',
    title: '新币上线',
    description: '发现 USD1 永续上新时通知，缺失额度补齐后再通知一次。',
  },
  {
    key: 'strategy_capacity_alerts',
    title: '策略额度达标',
    description:
      'XAU / SPCX / CL 的 5x、10x、20x 公开额度超过账户所设门槛时提醒；提醒不代表账户满足开仓条件。',
  },
  {
    key: 'listing_capacity_alerts',
    title: '最大杠杆额度',
    description: '下方选中币种在最大杠杆的公开可用额度大于零时提醒。',
  },
  {
    key: 'trade_summary_alerts',
    title: '普通开仓成交汇总',
    description: '普通开仓结束后按币种汇总成交；循环与迁移目前只记录本地历史。',
  },
];

const date = (value?: number | null) =>
  isDisplayTimestamp(value)
    ? new Date(value * 1000).toLocaleString('zh-CN', { hour12: false })
    : '—';

function SettingSwitch({
  title,
  description,
  checked,
  disabled,
  onChange,
}: {
  title: string;
  description: string;
  checked: boolean;
  disabled: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <div className="monitor-setting">
      <div>
        <strong>{title}</strong>
        <p>{description}</p>
      </div>
      <Switch
        aria-label={title}
        checked={checked}
        disabled={disabled}
        onCheckedChange={onChange}
      />
    </div>
  );
}

export function MonitoringPanel({
  monitoring,
  notification,
  demo,
  busy,
  connectionError,
  action,
}: {
  monitoring?: Monitoring;
  notification?: State['notification'];
  demo: boolean;
  busy: boolean;
  connectionError: string;
  action: DeskAction;
}) {
  const [query, setQuery] = useState('');
  const [pending, setPending] = useState(false);
  if (!monitoring)
    return (
      <section className="panel empty-state">
        <p>正在读取监控设置…</p>
      </section>
    );
  const settings = monitoring.settings;
  const summaryIntervalMinutes = settings.hourly_summary_interval_minutes ?? 60;
  const summary = notification?.hourly_summary;
  const scheduledChannel = notificationChannel(notification, 'scheduled');
  const summaryAvailable =
    !demo &&
    settings.feishu_enabled &&
    settings.hourly_summary_alerts &&
    scheduledChannel.configured;
  const summaryStatus = demo
    ? '模拟环境不发送定时摘要。'
    : !settings.feishu_enabled
      ? '飞书总开关已关闭，定时摘要暂停发送。'
      : !settings.hourly_summary_alerts
        ? '定时摘要已关闭。'
        : !scheduledChannel.configured
          ? '定时机器人未配置或配置无效，定时摘要暂停发送。'
          : summary?.pending
            ? scheduledChannel.error
              ? '本次摘要待发送，通知服务等待重试。'
              : '本次摘要待发送。'
            : summary?.next_due_at != null
              ? '等待下次计划发送。'
              : '等待服务器安排下次发送。';
  const disabled = busy || pending || Boolean(connectionError) || demo;
  const save = async (body: object, symbol?: string) => {
    if (disabled) return false;
    setPending(true);
    try {
      return await action(
        symbol
          ? `/api/monitoring/symbols/${encodeURIComponent(symbol)}`
          : '/api/monitoring',
        body,
        'PATCH',
      );
    } finally {
      setPending(false);
    }
  };
  const rows = monitoring.symbols.filter((row) =>
    row.symbol.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
  );
  const active = monitoring.symbols.filter(
    (row) => row.effective_monitor,
  ).length;
  return (
    <div className="monitoring-workspace">
      <section className="panel">
        <div className="section-head">
          <div>
            <h2>监控与告警</h2>
            <p>
              所有账户共用 · 开关修改即保存，摘要间隔需点击保存 ·
              服务器运行期间，关闭网页后仍按设置运行
            </p>
          </div>
          <output className="small-note">
            {pending ? '保存中…' : `${active} 个币种已纳入采样计划`}
          </output>
        </div>
        <div className="monitor-master-grid">
          <SettingSwitch
            title="独立币种监控"
            description="控制公开额度、盘口和币种详情的后台轮询。交易与订单核对所需数据会继续采样。"
            checked={settings.monitoring_enabled}
            disabled={disabled}
            onChange={(value) => void save({ monitoring_enabled: value })}
          />
          <SettingSwitch
            title="飞书告警总开关"
            description="控制本服务所有飞书消息。关闭会取消待发通知，重新开启不补发关闭期间的历史。"
            checked={settings.feishu_enabled}
            disabled={disabled}
            onChange={(value) => void save({ feishu_enabled: value })}
          />
        </div>
        <output className="monitor-status monitor-notification-status">
          {demo
            ? '模拟环境仅展示设置，不运行独立监控或发送飞书。'
            : !settings.feishu_enabled
              ? '飞书告警已关闭；下方类别和币种选择仍会保存。'
              : notificationOverview(notification)}
        </output>
        <div className="monitor-master-grid">
          {(['scheduled', 'event'] as const).map((key) => {
            const channel = notificationChannel(notification, key);
            return (
              <div className="monitor-setting" key={key}>
                <div>
                  <strong>
                    {key === 'scheduled'
                      ? '常态化定时机器人'
                      : '突发事件机器人'}
                  </strong>
                  <p>
                    {key === 'scheduled'
                      ? `定时运行摘要 · 每 ${summaryIntervalMinutes} 分钟`
                      : 'WS 故障与恢复、上新、额度达标、成交汇总'}
                  </p>
                  <p className={channel.error ? 'amber' : 'muted'}>
                    {channel.error ||
                      (channel.configured ? '已配置' : '未配置，暂停发送')}
                    {channel.source === 'legacy' ? ' · 沿用旧机器人' : ''}
                    {channel.pending != null
                      ? ` · ${channel.pending} 条待发送`
                      : ''}
                  </p>
                </div>
              </div>
            );
          })}
        </div>
        <p className="monitor-status">
          在主服务器运行 <code>sudo aster-desk feishu-configure</code>{' '}
          绑定两个机器人。 启用双通道后，未配置的通道不会转发到另一机器人。
        </p>
      </section>
      <section className="panel">
        <div className="section-head">
          <div>
            <h2>定时运行摘要</h2>
            <p>
              发送到定时机器人 ·
              覆盖所有账户与配对组，仅遵循飞书总开关与本摘要开关
            </p>
          </div>
        </div>
        <div className="monitor-master-grid">
          <SettingSwitch
            title="发送定时摘要"
            description="按已保存的间隔汇总运行状态、UTC 日交易量目标进度、持仓与保证金划转、API 预算及异常。"
            checked={settings.hourly_summary_alerts}
            disabled={disabled}
            onChange={(value) => void save({ hourly_summary_alerts: value })}
          />
          <div className="monitor-setting">
            <div>
              <strong>发送安排</strong>
              <p>
                已保存间隔：每 {summaryIntervalMinutes} 分钟
                <br />
                上次成功发送：{date(demo ? null : summary?.last_sent_at)}
                <br />
                下次计划：
                {date(summaryAvailable ? summary?.next_due_at : null)}
              </p>
            </div>
          </div>
          <SummaryIntervalForm
            savedMinutes={summaryIntervalMinutes}
            disabled={disabled}
            save={save}
          />
        </div>
        <output className="monitor-status monitor-notification-status">
          {connectionError ? '连接中断，以下为上次获取的发送状态。' : ''}
          {summaryStatus} 保存新间隔会取消待发摘要，并从保存时刻等待完整周期；
          保存相同间隔不改变安排。重新启用或通道恢复后也等待完整周期，不补发历史摘要。
        </output>
      </section>
      <section className="panel">
        <SettingSwitch
          title="副服务器 WS 异常告警"
          description="连续失败 3 次、断连持续 60 秒，或连接后 2 分钟没有新的有效额度样本时提醒。仅遵循飞书总开关与本开关，不受币种选择影响。"
          checked={settings.relay_health_alerts}
          disabled={disabled}
          onChange={(value) => void save({ relay_health_alerts: value })}
        />
        <p className="monitor-status">
          发送到事件机器人。每次故障只提醒一次；故障通知送达后，稳定恢复 30
          秒再通知恢复。 HTTP 补取成功不代表 WS 恢复，单次样本超过 1
          秒不会触发此告警。
        </p>
      </section>
      <section className="panel">
        <div className="section-head">
          <div>
            <h2>事件通知类型</h2>
            <p>发送到事件机器人 · 同时遵循飞书总开关与下方各币种的告警选择</p>
          </div>
        </div>
        <div className="monitor-category-grid">
          {categories.map((category) => (
            <SettingSwitch
              key={category.key}
              title={category.title}
              description={category.description}
              checked={settings[category.key]}
              disabled={disabled}
              onChange={(value) => void save({ [category.key]: value })}
            />
          ))}
        </div>
        {!monitoring.strategy_capacity_environment_enabled && (
          <p className="monitor-status amber">
            服务器环境配置已关闭策略额度提醒；网页开关开启后仍不会发送这一类通知。
          </p>
        )}
      </section>
      <section className="panel">
        <div className="section-head">
          <div>
            <h2>币种范围</h2>
            <p>公开额度提醒需要开启监控；成交汇总只遵循告警开关。</p>
          </div>
        </div>
        <div className="monitor-master-grid">
          <SettingSwitch
            title="扫描 USD1 新币"
            description="每 60 秒更新交易对目录；关闭后已有币种仍可采样。重新开启先建立基线。"
            checked={settings.discovery_enabled}
            disabled={disabled}
            onChange={(value) => void save({ discovery_enabled: value })}
          />
          <SettingSwitch
            title="自动监控新发现币种"
            description="仅影响以后发现的币种；关闭后可在列表中逐个选择。不会自动加入交易策略。"
            checked={settings.auto_monitor_new}
            disabled={disabled}
            onChange={(value) => void save({ auto_monitor_new: value })}
          />
        </div>
        <div className="monitor-filter">
          <Input
            aria-label="搜索监控币种"
            placeholder="搜索币种，例如 XAU、BTC"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
          <span className="small-note">
            {rows.length} / {monitoring.symbols.length} 个币种
          </span>
        </div>
        <div
          className="table-scroll"
          aria-label="币种监控与飞书开关，可横向滚动"
        >
          <Table className="monitor-symbol-table">
            <TableHeader>
              <TableRow>
                <TableHead>币种</TableHead>
                <TableHead>独立监控</TableHead>
                <TableHead>飞书告警</TableHead>
                <TableHead>最大杠杆额度 &gt; 0</TableHead>
                <TableHead>采样安排</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {rows.map((row) => (
                <TableRow key={row.symbol}>
                  <TableCell>
                    <strong>{row.symbol}</strong>
                    <small>
                      {row.strategy_market
                        ? '策略品种 · 5x / 10x / 20x'
                        : 'USD1 永续 · 最大杠杆'}
                      {row.status !== 'TRADING' ? ' · 非交易中' : ''}
                    </small>
                  </TableCell>
                  <TableCell>
                    <Switch
                      aria-label={`${row.symbol} 独立监控`}
                      checked={row.monitor}
                      disabled={disabled}
                      onCheckedChange={(value) =>
                        void save({ monitor: value }, row.symbol)
                      }
                    />
                  </TableCell>
                  <TableCell>
                    <Switch
                      aria-label={`${row.symbol} 飞书告警`}
                      checked={row.alerts}
                      disabled={disabled}
                      onCheckedChange={(value) =>
                        void save({ alerts: value }, row.symbol)
                      }
                    />
                  </TableCell>
                  <TableCell>
                    <label className="listing-watch-toggle">
                      <input
                        type="checkbox"
                        aria-label={`${row.symbol} 最大杠杆额度提醒`}
                        checked={row.max_capacity_alert}
                        disabled={disabled || !row.can_watch}
                        onChange={(event) =>
                          void save(
                            { max_capacity_alert: event.target.checked },
                            row.symbol,
                          )
                        }
                      />
                      {row.can_watch ? '额度大于零时提醒' : '等待目录收录'}
                    </label>
                  </TableCell>
                  <TableCell>
                    <span className={row.effective_monitor ? 'mint' : 'muted'}>
                      {row.required_by.length
                        ? '交易需要，保留采样'
                        : row.effective_monitor && row.status === 'TRADING'
                          ? '独立采样已启用'
                          : row.status !== 'TRADING' && row.effective_monitor
                            ? '等待恢复交易'
                            : '已停止独立采样'}
                    </span>
                    <small>
                      {row.required_by.length
                        ? `关联账户：${row.required_by.join('、')}`
                        : row.effective_monitor
                          ? row.strategy_market
                            ? '普通额度按预算采样；详情约 60 秒'
                            : '最大杠杆详情约 60 秒'
                          : '保留上次数据与采样时间'}
                    </small>
                    {row.max_capacity_alert &&
                      (!settings.feishu_enabled ||
                        !settings.listing_capacity_alerts ||
                        !row.alerts ||
                        !row.detail_enabled) && (
                        <small className="amber">
                          额度提醒暂停，选择已保留
                        </small>
                      )}
                  </TableCell>
                </TableRow>
              ))}
              {!rows.length && (
                <TableRow>
                  <TableCell colSpan={5}>
                    <p className="muted">没有匹配的币种，请调整搜索内容。</p>
                  </TableCell>
                </TableRow>
              )}
            </TableBody>
          </Table>
        </div>
        <p className="monitor-status">
          此处显示采样安排；采样是否成功及数据时间请查看「普通开仓」或「USD1
          上新」。关闭监控不会关闭账户或平仓。公共行情连接及账户核对按交易需求保留；已发出的飞书请求无法撤回。
        </p>
      </section>
    </div>
  );
}
