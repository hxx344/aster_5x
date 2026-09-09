# Linux 账户交易管理

前端和交易服务部署在同一台 Linux 服务器，由 `aster-desk` systemd 服务运行。无需 Windows 定时任务。支持 XAUUSD1、SPCXUSD1、CLUSD1，最多 8 个独立账户。代码具备实盘执行入口；部署本身不会创建或启动实盘账户。

## 安装与访问

推荐 Ubuntu 24.04 或 Debian 12，要求 systemd、Python 3.10+，能访问 GitHub、npm、PyPI、nodejs.org 和 Aster。安装脚本会补充系统依赖、Python 虚拟环境和构建所需的 Node；Node 官方下载包经过 SHA256 校验。

```bash
curl -fsSL https://raw.githubusercontent.com/hxx344/aster_5x/main/install-trading.sh | sudo bash
```

已有仓库时可以执行 `sudo bash install-trading.sh` 安装本地代码。首次安装会打印随机生成的网页访问密码，并保存到 `/etc/aster-desk/environment`（root 读写）。服务默认只监听服务器 `127.0.0.1:8765`。

在自己的电脑建立 SSH 隧道，再打开浏览器 `http://127.0.0.1:8765`：

```bash
ssh -N -L 8765:127.0.0.1:8765 YOUR_SERVER_USER@YOUR_SERVER_IP
```

使用域名时，在环境文件设置 `ASTER_PUBLIC_ORIGIN=https://你的域名`，用现有 HTTPS 反向代理转发到 `127.0.0.1:8765`，保留 Host 并传递 `X-Forwarded-Proto`。例如 Caddy：

```caddy
aster.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

## 接入实盘子账户

每个子账户有三项不可更改的固定前提：**全仓保证金模式、双向持仓模式、单币保证金模式（USD1）**。程序只读取核验，不提供修改这些模式的配置项、界面或操作。任一项不符时禁止启动；运行中检测到不符则持久化暂停该账户，保留未完成批次，并停止新开仓、升杠杆和补偿下单。模式恢复后仍需手动启动策略，已有批次可继续核对。网页逐项显示核验状态。已有仓位还需多空数量一致、没有挂单。

在服务器执行 `sudoedit /etc/aster-desk/environment`，填写每个账户在 Aster Pro API 页面提供的 user、signer、API agent 私钥。这里的私钥是 API 签名账户私钥，不是主钱包私钥。不要把密钥提交到 Git 或粘贴到网页。

```dotenv
ASTER_ALLOW_LIVE=1
ASTER_A_USER=0x账户地址
ASTER_A_SIGNER=0xAPI签名地址
ASTER_A_PRIVATE_KEY=0xAPI签名私钥
ASTER_B_USER=0x另一个账户地址
ASTER_B_SIGNER=0x另一个API签名地址
ASTER_B_PRIVATE_KEY=0x另一个API签名私钥
FEISHU_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/机器人地址
FEISHU_SIGN_SECRET=机器人的可选签名密钥
```

保存后执行 `sudo aster-desk restart`。登录网页，添加账户，模式选“实盘”，环境变量前缀填 `ASTER_A` 或 `ASTER_B`。账户默认暂停；读取成功后可检查实际持仓、杠杆、USD1 总权益和保证金占用率，再点击启动。修改服务器密钥需要重启服务。

每个账户可以设置额度阈值和每批每边的金额上限（默认 10,000 / 1,000 USD1）。最终仓位由风险、余额和额度决定；单批金额只是拆单上限，不是总持仓目标。模拟账户采用独立账本，不向交易所下单，也不发送飞书通知。

## 交易口径与执行

- 同时监控 4x、5x、10x、20x，以及账户实际杠杆对应的公开额度。**全局禁止降杠杆**，不论已有持仓、空仓还是重新启动，都只能升档或保持实际杠杆。空仓初始最低使用 4x；已经处于更高杠杆时不会重置到 4x。
- 每批多空等数量，盘口价差 `(ask-bid)/mid ≤ 0.0005`。以 BBO 限价 FOK 批量提交，每轮完成后重新读取账户；实际周期包含请求耗时和至少 5 秒等待。
- 每个仓位的占用保证金为 `|数量| × 标记价格 ÷ 该仓位实际杠杆`。账户全部 USD1 仓位的占用分别累加，多空不抵消，不同标的按各自杠杆计算。保证金占用率为 `总占用保证金 / (crossWalletBalance + crossUnPnl)`，分母为 USD1 全仓钱包余额加全仓未实现盈亏，不使用账户顶层 USDT 汇总字段。维持保证金不再作为开仓上限指标。
- 每笔同时考虑两边手续费、价差及标记价格偏差对成交后总权益的影响，并检查账户档位容量、可用余额、最小数量和盘口深度。预测成交后的保证金占用率必须**不超过 50%（允许恰好 50%）**；接近上限时自动缩小每批数量，无法容纳最小一笔时等待。每次成交后再读取账户核对实际占用率；超过 50% 时持久化暂停账户交易。启动时已有占用达到或超过 50%，仍可在满足原额度条件时先升杠杆释放占用；升杠杆本身不新增仓位。升档成功后按确认的实际杠杆重算已有仓位占用，只有预测成交后不超过 50% 才能新增仓位。
- 已有低杠杆持仓时，选择高于当前杠杆且满足条件的最低可用档位，可以跳过没有额度的中间档。目标档公开剩余额度必须严格大于配置阈值和多空总持仓名义价值，账户档位也必须容纳现有仓位。例如 XAU 已有 1x 仓位、4x 满足条件时，先升到 4x，读取账户确认成功，再按 4x 额度计算和分批加仓；不会因缺少 1x 公开额度而阻止升档。
- 多档同时可用时，升档确认后优先在该实际档位分批开仓，完成一批后再检查下一次升档；该顺序会保留到重启后。若当前档额度已不足，或风险、余额等不足以容纳最小一笔，则允许继续寻找可用的更高档，仍需满足原有开仓风控条件。持平直接使用当前档额度；只有较低档有额度时等待，绝不降档。提交杠杆调整前再次读取实际杠杆，旧降档批次保留并暂停核对，不会重发。
- 使用多空总名义价值检查容量，不把相等多空抵消成零。额度为公开估算，再结合认证账户风控档位；交易所仍可能因为实际占用或行情变化拒单。

两腿批量委托不是原子成交。如果只有一腿成交，程序核对实际持仓后只平掉本批新增的不平衡部分，最多自动补偿 3 次。超时或响应不确定时按已保存的 client order ID 查询，绝不重发原开仓订单；重启继续核对。补偿未完成或发现外部成交、ADL、持仓不符时暂停该账户，并显示原因。

“暂停策略”停止新开仓和新杠杆调整，已提交批次仍继续核对和必要补偿。要停止所有请求使用 `sudo aster-desk stop`。网页的“重新核对”不会重发原开仓单；它允许重新检查与再次补偿。如果订单始终查不到或外部仓位变化无法与回执对应，记录会保留，必须先在交易所核查原因；本版不提供一键丢弃未确认订单的功能。

标记价格和未实现盈亏变化会改变实际占用率，手续费与价差也会减少成交后权益。预测约束不保证任何时刻实际占用率都不超过 50%；程序检测到超限时暂停加仓，不会自动平掉整个现有组合。

## 飞书、升级与运维

完成一轮连续加仓后，暂停、达到风险限制，或连续 60 秒不能继续加仓时，汇总发送账户、成交批次、每个标的与杠杆、总名义金额及实际保证金占用率。通知队列持久化，失败重试；飞书已收到但响应丢失时可能重复。没有配置 Webhook 时保留待发消息，网页显示待发送数量。

```bash
sudo aster-desk status
sudo aster-desk logs
sudo aster-desk stop
sudo aster-desk start
sudo aster-desk restart
```

重复安装命令即可升级。先构建新版本，再切换 `/opt/aster-desk/current`；健康检查失败恢复旧服务。账户、策略开关、未完成批次和通知保存在 `/var/lib/aster-desk/trading.sqlite3`，环境文件和数据库不会被升级覆盖。重启后已启用策略会继续运行；升级前可在网页暂停账户。新交易服务健康后，安装脚本停用旧 `aster-5x` 纯监控服务，避免重复轮询；旧配置和历史保留，飞书设置需填入新环境文件。

备份时先停止服务，再备份 `/var/lib/aster-desk` 和 `/etc/aster-desk`，完成后启动服务。一个数据库只允许一个服务进程。不同数据库或服务器不会共享进程锁，不要把同一真实账户同时交给多个实例。

## 开发验证

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/python -m unittest discover -v
cd dashboard
npm ci
npm run typecheck
npm run lint
npm run build
cd ..
.venv/bin/python -m trading.server --demo
```

演示环境使用明确标记的模拟行情与独立账本，限制回环地址，拒绝添加实盘账户。前端静态文件由 Python 服务直接提供，不需要在生产环境运行 Node。认证账户接口、真实成交和飞书发送必须在配置服务器凭据后核实；自动测试使用模拟接口，不代表实盘已验收。

接口依据：[Aster V3 签名规则](https://asterdex.github.io/aster-api-website/futures-v3/general-info/)、[账户与交易接口](https://asterdex.github.io/aster-api-website/futures-v3/account%26trades/)、[市场数据](https://asterdex.github.io/aster-api-website/futures-v3/market-data/)。服务器需启用时间同步，以满足 V3 nonce 时效要求。
