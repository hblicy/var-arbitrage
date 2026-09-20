# Arcus、Bulk、RISEx 接入实施计划

> 执行方式：在当前隔离分支内使用 executing-plans 和 test-driven-development 逐项实施，不启动子代理。用户已确认本方案。

**Goal:** 新增三家交易所的行情、资金费率与盘口复核，移除 Nado、GRVT、OndoPerps、Backpack，保留其余五家现有行为。

**Architecture:** 沿用 MarketCollector、MarketDatum、采集器工厂及现有套利/通知流程。全部使用公开 REST，无账户凭据、下单、数据库迁移或新依赖。

**Tech Stack:** Python / curl_cffi / FastAPI / React / Vite / unittest / node:test。

## 已核实接口（2026-09-20）

| 所 | 市场 | 盘口 | 资金费与成交额 |
| --- | --- | --- | --- |
| Arcus | `https://api.arcus.xyz/v1/markets` | `/v1/l2OrderBook/BTC-USD?nLevels=100` | `nextFundingRate`，1h；`nextFundingAt` 秒；`volume24hNotional` USD；盘口 timestamp 微秒 |
| Bulk | `https://exchange-api.bulk.trade/api/v1/exchangeInfo` 数组；`/api/v1/ticker/SOL-USD` | `/api/v1/l2book?type=l2book&coin=SOL-USD&nlevels=100` | `fundingRate`，整点 1h；`quoteVolume` USD；ticker / 盘口 timestamp 实测纳秒 |
| RISEx | `https://api.rise.trade/v1/markets?force_refresh=true` | `/v1/orderbook?market_id=5&limit=100` | `current_funding_rate`，`funding_interval` 纳秒；`next_funding_time` 纳秒；`quote_volume_24h` USD |

Bulk 官方 SDK 的 `l2Book` 大小写错误会返回 400，最新 REST 文档要求 `l2book`。RISEx `predicted_funding_rate` 已废弃，`funding_rate_8h` 不可再次按 1h 放大。Arcus 接口已返回预测值，但旧文档仍称不支持；缺失预测值保留 None，不拿历史值替代。缺失价格、无效数值和畸形响应必须明确失败，零资金费率是有效值。

标准账户单边 taker 默认：Arcus 2.25 bps（主网 `/v1/feeTiers` 的 225 ppm）、Bulk 3.5 bps、RISEx 3.0 bps；允许沿用环境变量覆盖方式设置实际账户费率。

来源：
- https://docs.arcus.xyz/api-reference/public/get-markets
- https://docs.arcus.xyz/concepts/perpetuals/funding
- https://docs.bulk.trade/api-reference/getL2Book
- https://docs.bulk.trade/bulk-exchange/funding
- https://docs.bulk.trade/bulk-exchange/fees
- https://developer.rise.trade/reference/marketservice_getmarkets
- https://docs.risechain.com/docs/risex/trading/fees

## 实施与验收

- [x] `test_exchange_collectors.py`：用真实响应形状测试三家字段/单位转换、零负费率、缺失费率、过滤关闭市场、符号映射、盘口格式、过期报价和上游失败。先验证新交易所尚未注册导致测试失败。
- [x] `collectors/arcus.py`、`bulk.py`、`risex.py`：实现 `fetch_markets(symbols)` 与 `fetch_order_book(symbol, limit)`；有限并发、原始 timestamp 新鲜度检查、错误传播；复用现有连接池与关闭机制。
- [x] `config.py`、`collectors/factory.py`、`exchange_configs.json`：最终注册八家，新所默认开启；保留旧配置未知键忽略逻辑及原微信 webhook 变量名。
- [x] `api.py`、`monitor.py`：移除旧所 import；不改变核心计算、通知例外和公共接口。
- [x] `dashboard/src/App.jsx`、`PositionsModal.jsx`：替换默认交易所与显示名称；旧浏览器偏好按现有 server 列表合并方式自然忽略。
- [x] 删除已被替代、无调用的四家采集器及对应专属测试，移除 `nado_protocol`；更新 `env.example`、README 的交易所与费用说明，历史持仓数据不删除。
- [x] 验证 `python -m unittest discover -q`、`node --test dashboard/src/*.test.mjs`、`npm run lint`、`npm run build`。
- [x] 三家主网只读冒烟：行情与盘口非空、资金费周期正确、复核可执行；通过 mock 通知验证新路由且不发真实 webhook。运行 API 验证最终交易所清单、前端与老配置；复查 git diff。

基线：09499f8；创建启动所需 logs 目录后 Python 38 项、前端 8 项通过。原仓库的 README/codex.md/AUDIT.md 未提交内容保持原状。

## 完成记录

- 新增三家采集器，移除四家采集器、专属配置/测试与 nado_protocol；保留微信历史变量名、原始持仓记录和五家现有交易所。
- RISEx 额外过滤 unlocked=false、reduce_only、post_only 市场，避免将不能吃单开仓的市场当作可执行路线。
- 只读独立审查发现新采集器首次使用的 `_gather_with_semaphore` 会在首个失败后遗留同批请求。增加两个先红后绿回归测试，并在 `collectors/base.py` 取消、等待和关闭同批任务/协程；审查复测峰值并发 5、失败后活跃请求 0、取消后遗留子任务 0。保留旧采集器没有调用此私有方法。
- `python -m unittest discover -q`：55 项通过（原 38 项中移除旧所专属 2 项，新增 19 项）。
- `node --test dashboard/src/opportunityViewModel.test.mjs dashboard/src/App.structure.test.mjs`：8 项通过。
- `npm run lint`、`npm run build`：通过。使用原锁文件安装依赖，未升级；本机 Node 21.2.0 不在 Vite 声明的版本范围内，安装有 engine 警告，但构建成功。
- 最终三家主网并发全扫描：Arcus 58、Bulk 19、RISEx 32 个市场；BTC/SOL 共生成 12 个方向的可计算机会，全部标记支持盘口复核。
- Arcus–Bulk、Bulk–RISEx、RISEx–Arcus 三组真实 BTC 两腿报价均成功计算 VWAP；当时净收益不足，正确返回 `INSUFFICIENT_EDGE`，不等于可获利信号。
- 通过 ASGI 验证 `/api/exchanges` 仅返回八家；通过 mock webhook 验证新增三家分别与 Binance 的分析、复核、通知链路，未发真实通知。
- `git diff --check` 无空白错误。未进行浏览器端视觉验收；本次前端仅列表与名称修改，已完成前端测试、lint、构建和后端列表接口验证。
- 原目录的 README.md 修改、codex.md 删除、AUDIT.md 未跟踪状态保持原状。实现留在隔离工作区，未推送或部署。
