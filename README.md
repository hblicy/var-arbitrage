# 跨交易所价差与资金费率监控 (var-arbitrage_v1.6)

此工具用于实时监控多个去中心化交易所（DEX）之间的**价格差异**与**资金费率差异**，帮助发现有利可图的跨所套利机会。

## 🔹 支持的交易所

- **Nado** (`nado`)
- **Variational** (`variational`) - 支持基于网页分析的零滑点监控
- **Binance** (`binance`) - 作为流动性最大基准
- **Lighter** (`lighter`)
- **Hyperliquid** (`hyperliquid`) - **NEW in v1.6!** 深度流动性，1h 资金费率结算
- **EdgeX** (`edgex`)
- **Backpack** (`backpack`)
- **GRVT** (`grvt`)

*(在 v1.6 中，Paradex 被移除并替换为 Hyperliquid，以获得更大的日均交易量和更好的价差捕捉能力。)*

## 🔹 核心功能

1. **多维度价差计算**：不仅仅看最新价，更结合最佳买卖价 (Bid/Ask) 以及各所设定的单边吃单手续费 (Taker Fee) 计算净盈亏空间 (Net Spread)。
2. **资金费率对齐**：各所资金费结算频率不同（1h / 4h / 8h 等），监控器内部自动折算为**统一的年化收益或 8h 基准值**，确保费率对比 100% 数学严谨。
3. **僵尸代币/假同名过滤 (MAX_PRICE_DEVIATION)**：如果两家交易所同名代币价格差异离谱（例如大于 15%），监控器会自动判定它们不是同一个标的（即发错网络或恶意空投的假币），直接跳过预警，防止资金损失。
4. **多通道告警**：支持**企业微信**、**Telegram**、**飞书 (Lark)** 等多渠道通知。可自定义触发告警的各种阈值。
5. **热更新面板 (Dashboard)**：支持浏览器可视化页面，可以直观查看所有套利对，且提供 `exchange_configs.json` 级别的服务无需重启热重载能力。

## 🔹 快速开始

### 1. 配置环境变量

复制 `env.example` 为 `.env`，再修改里面的 webhook、登录密码和阈值：

```bash
cp env.example .env
nano .env

# 如果 .env 是从 Windows 复制到 Linux，先清理 Windows 换行符
sed -i 's/\r$//' .env
```

至少建议修改：

```ini
NADO_VARIATIONAL_WECHAT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=你的key
POSITION_USER=admin
POSITION_PASS=请改成强密码
ARBITRAGE_SCAN_INTERVAL=10
DASHBOARD_MAX_COVER_HOURS=24
NOTIFY_MAX_COVER_HOURS=24
```

### 2. VPS 安装 / 升级 Node.js 和 npm

Web 面板是 Vite 前端，需要 Node.js 20+。如果 VPS 上是 Node 12/14，构建时可能报：

```text
SyntaxError: Unexpected reserved word
```

先查看版本：

```bash
node -v
npm -v
```

推荐安装 Node.js 20：

```bash
sudo apt update
sudo apt install -y curl ca-certificates
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

如果安装时报 `libnode-dev` 或旧版 `nodejs` 文件冲突，先清理旧包：

```bash
sudo apt remove -y libnode-dev nodejs npm
sudo apt autoremove -y
sudo apt --fix-broken install -y

curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

如果仍然被 `libnode-dev` 卡住，再强制移除冲突包：

```bash
sudo dpkg -r --force-depends libnode-dev
sudo apt --fix-broken install -y
sudo apt install -y nodejs
```

确认版本：

```bash
node -v
npm -v
```

正常应看到 `node` 为 `v20.x`，并且 `npm` 可以正常输出版本。

### 3. 构建 Web 面板

`dashboard/dist` 是构建产物，不会随 git 仓库提交。首次部署或前端代码更新后，需要构建一次：

```bash
cd /home/ubuntu/var-arbitrage/dashboard
rm -rf node_modules
npm ci
npm run build
```

说明：

- `npm ci` 会按 `package-lock.json` 安装依赖，适合服务器部署。
- `npm audit` 提示可以先忽略，不要直接执行 `npm audit fix`，避免自动改依赖版本。
- 新版 `start.sh` 会在缺少 `dashboard/dist/index.html` 或前端源码更新后自动构建面板。

### 4. 启动服务

```bash
cd /home/ubuntu/var-arbitrage
source .venv/bin/activate
pip install -r requirements.txt
./start.sh
```

默认会在 `http://服务器公网IP:8011` 提供 API 和 Web 面板。
如果浏览器只看到 `{"detail":"Not Found"}`，通常是前端还没有构建成功，请检查：

```bash
ls -la dashboard/dist
tail -100 logs/api.log
```

## 🔹 系统参数详解

- **`MAX_PRICE_DEVIATION_PCT` (新增防坑功能)**：如果在 Binance 上某币 10 块，但在另一个小 DEX 上才 1 块，普通监控器会报 900% 极品差价套利。开启它后（默认15%），当两边差价大于 15% 直接判定是同名山寨币而忽略。
- **`HYPERLIQUID_HTTP_TIMEOUT`**：设置对 Hyperliquid REST 节点的请求超时等待。
- **Taker Fee (吃单手续费设为零或不设的情况)**：很多 DEX 以 0 gas 或者限免 Taker 费作为宣传。如果你发现在某所需要付出 Taker 成本，请在 `config.py` 中的 `taker_bps` 进行精准修改（例如 Binance 默认是 5.0 bps）。

## 🔹 目录结构说明

- `api.py`: FastAPI Web 服务入口，承载 Dashboard 的静态资源和 WebSocket 连接。
- `analyzer.py`: **核心套利引擎**。两两枚举各所币种，包含严格的流动性过滤 (`min_volume_usd`) 和防假币碰撞过滤 (`max_price_deviation_pct`)。
- `config.py`: **交易所大全集**。各个交易所独立的数据转换模型（Endpoint 配置、默认费率等），在此可开关。
- `collectors/`: **异构数据抓取层**。比如 `hyperliquid.py`、`binance.py`，全部继承 `MarketCollector` 以抹平各 API 协议差异（REST, GraphQL, 网页抓取解析等）。
- `dashboard/`: WebUI 纯静态前端文件（HTML, JS, CSS）。

## 🔹 免责声明
加密货币衍生品交易具有极大风险。此服务提供的所有 "Spread（差价）" 和 "Funding Rate（资金费）" 均基于当下接口返回快照估算，网络延迟和吃单深度的不足都可能导致真实交易滑点远大于预期。请小资金测试确然后才用。
