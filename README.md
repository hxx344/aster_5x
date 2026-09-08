# Aster XAUUSD1 5x 额度监控

每 5 秒查询网页实际使用的公开风控接口，严格大于 **10,000 USD1** 时触发。飞书目前关闭，触发只记入本地日志。无需第三方 Python 依赖。

## 数据口径

读取 `leverageOiRemainingMap["5"]`，并取它与 5x 风控档位 `bracketNotionalCap` 的较小值。它是**不含个人持仓、挂单占用的公开可开额度**。网页会进一步扣除账户占用，因此公开额度达标不保证你的账户能开出同等仓位。程序不会登录账户、调整杠杆或下单。

2026-09-09 对照网页公开 JavaScript 核对了以下接口与计算方式，并完成无认证实测：

- `GET https://www.asterdex.com/bapi/futures/v1/public/future/common/symbol/leverageoi/remaining?symbol=XAUUSD1`
- `POST https://www.asterdex.com/bapi/futures/v1/friendly/future/common/brackets`，查询体 `{"symbol":"XAUUSD1"}`。
- 网页模块 `useTicker-iKEcy8XC.js` 和 `shared~...~e3kb53wr-C_GIamnV.js` 使用上述额度映射和风控档位，再扣个人占用。这些网页接口可能随站点变更；字段缺失或异常时程序报错，不把失败当作 0。
- [官方 API 文档](https://asterdex.github.io/aster-api-website/futures/account%26trades/#remaining-openable-notional-value-user_data)另有账户查询接口，本实现使用已核实的网页公开接口。

## 运行与检查

需要 Python 3.11+，在 PowerShell 执行：

```powershell
cd D:\project_aster_5x
python -m unittest -v
python monitor.py --once
.\start.ps1
Get-Content .\runtime\status.json
Get-Content .\runtime\monitor.log -Tail 10
```

`--once` 会运行一次完整检查，若启用飞书且达标也会推送。后台监控已启动时不要同时运行它，单实例锁会阻止重复进程。`start.ps1` 以隐藏窗口启动；关掉终端不会停止它，但关机、休眠会中断监控。本项目不自动设置开机启动。

停止：`python monitor.py --stop`。再次启动用 `start.ps1`。配置修改后需要停止并重启。`runtime/status.json` 包含 PID、检查时间、当前值、阈值、运行状态和推送开关；只有 `status=ok` 且检查时间新鲜时才表示最新查询成功。错误状态保留上次成功值和时间，不能当成当前值。

5 秒为目标检查周期，网络请求、接口限流和重试会增加延迟，轮询之间的短暂波动可能漏过。失败指数退避，429 至少等 180 秒，418 至少等 24 小时。`Retry-After` 秒数更长时遵从服务端。

## 飞书配置（目前未启用）

日后在已忽略的 `config.local.json` 中填写：

```json
{
  "feishu_enabled": true,
  "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/替换为机器人地址",
  "feishu_sign_secret": "如未开启签名校验则留空"
}
```

也可通过 `FEISHU_WEBHOOK_URL`、`FEISHU_SIGN_SECRET` 环境变量提供密钥，但仍需设置 `feishu_enabled: true`。支持[飞书官方机器人签名](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。日志不记录 Webhook 或签名密钥。若机器人启用了关键词校验，可使用消息中的 `Aster`。

首次检查已达标会提醒；持续达标只提醒一次；回落至阈值及以下后重新触发，两次提醒至少间隔 300 秒。冷却期间发生的新触发只在冷却结束后仍达标时发送。成功记录持久化，重启避免重复；发送失败会重试。飞书已接收但响应丢失或发送后进程崩溃时，重试仍可能产生重复消息。
