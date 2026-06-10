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

复制 `.env.example` (如果存在) 为 `.env` 或直接在项目根目录下编辑 `.env` 文件：

```ini
# --- 核心通知配置 ---
# 建议配置至少一种通知渠道
NADO_VARIATIONAL_WECHAT_WEBHOOK=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx
# TG_BOT_TOKEN=xxxxxxxx:yyyyyyy
# TG_CHAT_ID=-123456789
# LARK_WEBHOOK_URL=https://open.feishu.cn/open-apis/bot/v2/hook/xxxx

# --- 监控与显示门槛 ---
# 只有价差、费率差距、综合差距同时大于以下设定 (基点) 时，面板才会高亮显示
DASHBOARD_MIN_SPREAD_BPS=20
DASHBOARD_MIN_FUNDING_BPS=10
DASHBOARD_MIN_TOTAL_BPS=20

# 只有价差、费率差距、综合差距同时大于以下设定 (基点) 时，才会触发微信/TG发送消息！
NOTIFY_MIN_SPREAD_BPS=100
NOTIFY_MIN_FUNDING_BPS=10
NOTIFY_MIN_TOTAL_BPS=100

# 过滤掉 24 小时成交额过低的冷门币（默认过滤低于 200,000 USD 的小币）
GLOBAL_MIN_VOLUME_USD=200000

# 最大价格容差过滤：防止某所上线同名假币导致虚假套利机会（例如 15.0%）
MAX_PRICE_DEVIATION_PCT=15.0
```

### 2. 启动服务

**启动后台采集与 API 提供服务**：
```bash
python api.py
```
默认会在本地 `http://127.0.0.1:8000` 启动接口服务与 Web 面板。
直接用浏览器打开上述地址即可查看实时行情对比面板。

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
