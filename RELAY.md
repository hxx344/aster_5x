# 公开额度副服务器

副服务器通过自己的公网出口采集 Aster 公开剩余额度和杠杆档位，通过加密 WebSocket 推送给主服务器。HTTP 备用读取同一份缓存，不额外请求 Aster。副服务器不配置交易账户、钱包或签名密钥。

## 一键安装副服务器

使用独立公网 IP 的 Debian 12+、Ubuntu 24.04+ 或其他提供 Python 3.11+ 和 systemd 的 Linux 系统：

```bash
curl -fsSL https://raw.githubusercontent.com/hxx344/aster_5x/main/install-relay.sh | sudo bash -s -- --address 你的副服务器公网IP
```

也可以省略 `--address`，首次安装时在终端输入公网 IP。安装器自动安装缺少的依赖、生成随机访问令牌和包含该 IP 的自签 TLS 证书、注册服务并启动。主服务器通过导入证书验证副服务器身份，不需要域名。请在服务器已有的防火墙或云安全组中允许主服务器访问副服务器 TCP 8766。

配置保存在 `/etc/aster-capacity-relay/environment`，默认只采集 `XAUUSD1`，剩余额度间隔 0.2 秒，杠杆档位间隔 60 秒。需要其他市场时修改 `ASTER_RELAY_SYMBOLS`，例如 `XAUUSD1,SPCXUSD1,CLUSD1`，然后执行 `sudo aster-capacity-relay restart`。每个市场使用同一个采集间隔。未配置的市场和额外 USD1 上新额度详情会明确显示不可用；主服务器不会为这些市场自行改为直连 Aster。

## 连接主服务器

主服务器先用原有 `install-trading.sh` 升级到包含副服功能的版本。然后在副服务器运行：

```bash
sudo aster-capacity-relay export-connect
```

它会输出一行包含地址、访问令牌和公开证书的连接 JSON。把这一行复制到主服务器，在下面命令的隐藏输入提示中粘贴并回车：

```bash
sudo aster-desk relay-connect
```

也可以把连接 JSON 放入仅自己可读的文件后执行 `sudo aster-desk relay-connect --file /path/to/connection.json`。连接信息包含访问令牌，请直接在两台服务器间传递，不放进仓库、群聊或公开日志。

导入会先验证证书、地址和令牌，只更新 `/etc/aster-desk/environment` 中三项 `ASTER_CAPACITY_RELAY_URL`、`ASTER_CAPACITY_RELAY_TOKEN`、`ASTER_CAPACITY_RELAY_CA_FILE`，保留其他配置、账户和交易数据。证书写入 `/etc/aster-desk/relay-ca.pem`。配置有变化时重启主服务；主服务健康检查失败会恢复导入前的配置并尝试重新启动。

## 查看状态与升级

主服务器工作台标题下方的“副服务器额度连接”显示 WS 连接与缓存新鲜度，点击“详情”可查看最近连接/断开、消息接收和新样本接纳时间、重连等待、HTTP 补取次数/失败/最近响应，以及各交易对样本的来源与年龄。连接状态来自主服务器，不是浏览器自己的 WS 连接；页面沿用约 3 秒一次的状态刷新，不额外请求 Aster。只显示已收到的样本，不能据此判断全部目标交易对已覆盖。

WS 已连接与样本有效分开显示：循环公开额度要求 1 秒内，普通额度缓存最长 8 秒，杠杆档位最长 5 分钟。主页面失联或状态超过 8 秒未更新时标记“最近记录”，不将旧状态显示为实时正常。没有配置副服和旧版服务未提供详情也分别标注。此状态详情功能只需升级主服务器，副服务器无需重装。

```bash
sudo aster-capacity-relay status
sudo aster-capacity-relay logs
```

`status` 同时显示服务状态与缓存采样年龄。健康检查仅检查服务可用性；上游暂时失败时服务仍可运行，原样本继续自然过期，不会续成新数据。

再次执行原安装命令即可升级。相同提交跳过源码压缩包下载；相同代码、依赖和配置跳过依赖安装、验证和重启。代码变化复用匹配 Python 版本与锁文件的依赖环境，先验证新版本再切换，健康检查失败自动回退。现有令牌、证书和配置会保留。程序保存在 `/opt/aster-capacity-relay`，与主服务器的数据目录独立。

远端模式释放主服务器原本用于公开额度请求的 Aster 预算，WS 中断时尝试副服 HTTP 缓存；两者都没有新鲜数据时暂停依赖额度的新增仓位，既有订单核对和恢复逻辑继续运行。主服仍按原策略频率消费额度；V1 副服按固定配置采集，主服达成当日目标不会自动改变副服采集频率。

两台服务器保持系统自动校时；样本采集时间、传输延迟和时钟偏差会保守计入年龄，循环策略只使用 1 秒内的额度。增加采集市场后，各市场共享副服采集预算，实际间隔可能变长；可以通过副服状态查看采样年龄。

要恢复主服务器直接采集：

```bash
sudo aster-desk relay-disconnect
```

此命令移除三项连接设置、验证主服务重启结果并保留其他配置。它不会停止副服务器，可按需在副服务器执行 `sudo aster-capacity-relay stop`。
