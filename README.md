# Aster 多账户交易管理与额度监控

新增 Linux 网页账户管理：多账户双向市价分批开仓，监控 **4x / 5x / 10x / 20x + 最低开仓杠杆 + 实际杠杆**。每账户可设置最低开仓杠杆（1–125，默认 **4x**）和风险约束上限（大于 0%、不超过 100%，默认 **50%**），暂停策略后在网页“策略设置”修改。BBO 价差不超过万 5。每仓占用保证金为 `|数量| × 标记价格 ÷ 实际杠杆`，全部仓位多空分别累加后除以 USD1 账户总权益，预测成交后的占用率不超过该账户设置。升杠杆后按新杠杆重算占用和额度并继续加仓，完成后汇总飞书通知。实盘凭据只放服务器环境变量。完整安装、配置和执行规则见 [Linux 交易部署说明](DEPLOYMENT.md)。

```bash
curl -fsSL https://raw.githubusercontent.com/hxx344/aster_5x/main/install-trading.sh | sudo bash
```

新版 `aster-desk` 同时支持**公共额度提醒**和**实盘交易完成汇总**，共用 `FEISHU_WEBHOOK_URL` 与可选的 `FEISHU_SIGN_SECRET`。已有机器人配置升级后直接沿用。公共提醒复用现有行情轮询，监控三个标的的 4x / 5x / 10x / 20x，默认严格大于 10,000 USD1 时提醒，与账户数量、账户开仓阈值和交易启停互相独立，不增加 Aster 查询。

公共提醒默认启用，可在 `/etc/aster-desk/environment` 设置 `ASTER_CAPACITY_ALERT_ENABLED=0` 单独关闭；自定义阈值与冷却时间分别使用 `ASTER_CAPACITY_ALERT_THRESHOLD=10000`、`ASTER_CAPACITY_ALERT_COOLDOWN_SECONDS=300`，修改后执行 `sudo aster-desk restart`。每个组合首次达标提醒一次，持续高位不刷屏，回落后重新达标且冷却结束才再提醒。成功状态持久化，失败只在新额度仍有效且达标时重试；未配置 Webhook 时不积累公共提醒历史，演示模式不发送。交易汇总优先发送，HTTP 投递结果不确定时仍可能重复。提醒使用未扣个人占用的公开估算，完整说明见 [飞书、升级与运维](DEPLOYMENT.md#飞书升级与运维)。

升级保留新版环境文件和数据库，安装脚本会停用旧 `aster-5x` 服务以免重复轮询。新版不启动旧 `monitor.py`，也不自动导入旧 `alerts.json` 的提醒状态；首次启用时可能对当前达标额度提醒，之后重启沿用新的成功状态。

以下是保留的**旧版独立额度监控工具**说明，其安装命令、配置和历史迁移规则只适用于 `aster-5x`，与新版 `aster-desk` 分开管理。旧工具不会登录账户或下单，同时监控 **XAUUSD1、SPCXUSD1、CLUSD1**，每个标的按 **4x / 5x / 10x / 20x、每 5 秒检查、严格大于 10,000 USD1** 的规则独立触发。默认关闭飞书，触发只记入本地日志。纯监控无需第三方 Python 依赖。

十二个“标的 + 杠杆”组合分别保存提醒状态和冷却时间；同一标的各档位共用一次接口快照，请求量不增加，一个组合的持续达标不会压制其他组合的提醒；普通网络失败只让对应标的退避，其他标的继续检查。遇到 429/418 限流则暂停新的查询请求，遵守服务端退避时间。

## 旧版独立监控：Linux 一键安装

在使用 systemd 的 Linux 服务器上执行（推荐 Ubuntu 22.04+、Debian 12+、RHEL 9+）：

```bash
curl -fsSL https://raw.githubusercontent.com/hxx344/aster_5x/main/install.sh | sudo bash
```

已经是 root 用户时去掉 `sudo`。服务器需要能访问 GitHub 和 Aster；若没有 `curl`，先运行 `sudo apt-get update && sudo apt-get install -y curl`（Ubuntu/Debian），或 `sudo dnf install -y curl`（RHEL 系列）。其余缺少的依赖由脚本安装，系统 Python 需至少 3.9。

首次安装按提示填写，全部回车即可采用默认值：

1. 阈值：`10000` USD1。
2. 检查间隔：`5` 秒。
3. 飞书 Webhook：可留空；填写后自动启用，再输入可选签名密钥。密钥输入不显示。

安装后服务立即启动、开机自动运行、异常退出自动重启。后续无需编辑 JSON：

```bash
sudo aster-5x status        # 当前额度、查询状态、阈值和飞书开关
sudo aster-5x configure     # 交互修改配置，保存后自动重启
sudo aster-5x logs          # 跟踪日志；Ctrl+C 只退出日志查看
sudo aster-5x test-feishu   # 手动发送一条标明“测试”的消息
sudo aster-5x stop          # 停止监控
sudo aster-5x start         # 恢复监控
sudo aster-5x restart       # 重启监控
```

配置时回车保留当前值；Webhook 输入 `-` 可关闭飞书并清除保存的地址与密钥。安装不会发送测试消息；启用飞书后，若第一次查询已达标，会发送正常额度提醒。

**升级：重新执行安装命令，配置与提醒历史会保留，默认监控三个标的的 4x / 5x / 10x / 20x。** 原有三个标的的 5x 提醒状态会自动迁移，新增档位单独建立提醒记录，阈值、间隔和飞书设置继续沿用。如果手动设置了 `symbols` 列表，则只监控该列表；支持上述三个代码。旧配置中的 `leverage: 5` 会自动升级为四个档位；如需手动限制档位，可设置 `leverages: [4]` 或 `[5]`。服务与管理命令仍叫 `aster-5x`。无交互安装可用 `curl -fsSL https://raw.githubusercontent.com/hxx344/aster_5x/main/install.sh | sudo bash -s -- --non-interactive`，之后通过 `sudo aster-5x configure` 配置。已克隆仓库时也可执行 `sudo bash install.sh`，直接安装当前目录的代码。

程序位于 `/opt/aster-5x`，配置位于 `/etc/aster-5x/config.json`，状态和轮转日志位于 `/var/lib/aster-5x`。服务以独立 `aster-5x` 用户运行。`status` 分别显示十二个组合的额度、检查时间和错误；只有所有组合数据新鲜且成功时才返回成功。部分成功显示 `partial`，不把单个成功当成全部正常。首次查询失败时安装命令会返回失败并显示原因，但已安装的服务仍会按退避规则重试；用 `logs` 排查。查看 systemd 原始状态：`sudo systemctl status aster-5x`。

## 数据口径

分别读取 `leverageOiRemainingMap` 中配置的 `4`、`5`、`10`、`20` 档位，各自取它与对应杠杆风控档位 `bracketNotionalCap` 的较小值。它是**不含个人持仓、挂单占用的公开可开额度**。网页会进一步扣除账户占用，因此公开额度达标不保证你的账户能开出同等仓位。程序不会登录账户、调整杠杆或下单。

2026-09-09 对照网页公开 JavaScript 核对了以下接口与计算方式，并完成无认证实测：

- `GET https://www.asterdex.com/bapi/futures/v1/public/future/common/symbol/leverageoi/remaining?symbol=XAUUSD1`
- `POST https://www.asterdex.com/bapi/futures/v1/friendly/future/common/brackets`，查询体 `{"symbol":"XAUUSD1"}`。
- 网页模块 `useTicker-iKEcy8XC.js` 和 `shared~...~e3kb53wr-C_GIamnV.js` 使用上述额度映射和风控档位，再扣个人占用。这些网页接口可能随站点变更；字段缺失或异常时程序报错，不把失败当作 0。
- [官方 API 文档](https://asterdex.github.io/aster-api-website/futures/account%26trades/#remaining-openable-notional-value-user_data)另有账户查询接口，本实现使用已核实的网页公开接口。

## Windows 运行与检查

需要 Python 3.9+，在 PowerShell 执行：

```powershell
cd D:\project_aster_5x
python -m unittest -v
python monitor.py --once
.\start.ps1
Get-Content .\runtime\status.json
Get-Content .\runtime\monitor.log -Tail 10
```

`--once` 会对所有配置标的各运行一次检查，若启用飞书且达标也会推送。后台监控已启动时不要同时运行它，单实例锁会阻止重复进程。`start.ps1` 以隐藏窗口启动；关掉终端不会停止它，但关机、休眠会中断监控。Windows 不自动设置开机启动。

停止：`python monitor.py --stop`。再次启动用 `start.ps1`。配置修改后需要停止并重启。`runtime/status.json` 的 `markets` 按 `交易代码:杠杆`（例如 `XAUUSD1:4`、`XAUUSD1:5`）保存检查时间、当前值与运行状态；顶层保存 PID、标的列表、`leverages` 列表、阈值和推送开关。只有对应标的 `status=ok` 且检查时间新鲜时才表示最新查询成功。错误状态保留上次成功值和时间，不能当成当前值。

5 秒为目标检查周期，网络请求、接口限流和重试会增加延迟，轮询之间的短暂波动可能漏过。失败指数退避，429 至少等 180 秒，418 至少等 24 小时。`Retry-After` 秒数更长时遵从服务端。

## 手动飞书配置（可选）

Linux 推荐使用 `sudo aster-5x configure`；手动配置时编辑 `/etc/aster-5x/config.json`。Windows 在已忽略的 `config.local.json` 中填写：

```json
{
  "feishu_enabled": true,
  "feishu_webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/替换为机器人地址",
  "feishu_sign_secret": "如未开启签名校验则留空"
}
```

也可通过 `FEISHU_WEBHOOK_URL`、`FEISHU_SIGN_SECRET` 环境变量提供密钥，但仍需设置 `feishu_enabled: true`。支持[飞书官方机器人签名](https://open.feishu.cn/document/client-docs/bot-v3/add-custom-bot)。日志不记录 Webhook 或签名密钥。若机器人启用了关键词校验，可使用消息中的 `Aster`。

每个“标的 + 杠杆”组合首次检查已达标会提醒；持续达标只提醒一次；回落至阈值及以下后重新触发，同一组合两次提醒至少间隔 300 秒。冷却期间发生的新触发只在冷却结束后仍达标时发送。成功记录持久化，重启避免重复；发送失败会重试。飞书消息包含对应交易代码、杠杆和交易页面链接。飞书已接收但响应丢失或发送后进程崩溃时，重试仍可能产生重复消息。
