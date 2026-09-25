'use client';
import { useState } from 'react';
import type {
  DeskAction,
  Monitoring,
  MonitoringSettings,
  State,
} from '@/lib/desk-types';
import { Input } from '@/components/ui/input';
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
  key: keyof MonitoringSettings;
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
    description: 'XAU / SPCX / CL 的 5x、10x、20x 额度超过账户所设阈值时提醒。',
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
  const disabled = busy || pending || Boolean(connectionError) || demo;
  const save = async (body: object, symbol?: string) => {
    if (disabled) return;
    setPending(true);
    try {
      await action(
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
            <p>所有账户共用 · 修改即保存 · 关闭网页后仍按设置运行</p>
          </div>
          <output className="small-note">
            {pending ? '保存中…' : `${active} 个币种采样中`}
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
              : notification?.error ||
                (notification?.configured
                  ? `飞书已配置 · ${notification.pending} 条待发送`
                  : '飞书未配置，请在服务器设置 FEISHU_WEBHOOK_URL。')}
        </output>
      </section>
      <section className="panel">
        <div className="section-head">
          <div>
            <h2>飞书通知类型</h2>
            <p>同时遵循飞书总开关与下方各币种的告警选择</p>
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
                <TableHead>实际采样状态</TableHead>
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
                        ? '交易需要，继续采样'
                        : row.effective_monitor && row.status === 'TRADING'
                          ? '独立监控中'
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
          关闭监控不会关闭账户或平仓。公共行情连接及账户核对按交易需求保留；已发出的飞书请求无法撤回。
        </p>
      </section>
    </div>
  );
}
