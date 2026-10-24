# TxFlow、QFEX、PopDEX 接入实施计划

用户已确认：全部永续及 RWA；非美元合约保留原币报价，仅同币种配对；沿用只读监控，不接交易写入。开发在 `codex/add-three-exchanges-20261024`，不启动开发子代理。

## 实施与验收

- [x] 用真实公开响应形状补充采集器测试：注册、分页、原始符号映射、资金费单位与周期、报价时间、零数量档位、接口业务错误。
- [x] 新增三个 `MarketCollector`，复用现有连接池和 WebSocket 依赖；QFEX 获取批量报价，PopDEX 分页读取行情与资金费，TxFlow 单次批量资金费订阅并用共享 40/s 预算获取符合现有成交额门槛的 L2 报价。
- [x] 增加原计价币及资金费时段元数据；同标的同计价币才能配对；非美元数据不进入美元金额估算。
- [x] 按 QFEX 产品类别配置吃单费，贯通分析、策略、人工复核和持仓估算；既有交易所费用不变。
- [x] 根据 QFEX 返回的 IANA 时区和市场时段评估结算窗口，覆盖休市、跨日及夏令时；无法确认的窗口停止收益评估及自动信号。
- [x] 注册配置与工厂，添加面板默认名单、持仓选所及错误文案，补充环境变量和 README。
- [x] 执行完整 Python / Node 回归、前端 lint/build、三家主网只读冒烟及最终 diff 检查。

## 验证结果（2026-10-24）

- Python 161 项通过，Node 24 项通过；前端 lint、生产构建及 `git diff --check` 通过。
- 主网只读样本：TxFlow 236 个、QFEX 229 个、PopDEX 66 个市场；QFEX 8 个非美元合约保留 CNY/KRW/JPY/SEK 标签。
- 静态首页、`/api/exchanges`、`/api/data`、`/api/funding` 实际 ASGI 请求均返回 200，交易所清单共 11 家。
- 三家 L2 均单独读取成功；真实双腿复核样本因源报价过期或两腿时间差返回拒绝，保留原有 2 秒报价门槛与 1 秒两腿时差门槛，没有放宽条件以通过验证。
- 未发送真实通知、未提交交易；开发及审查均在隔离工作树完成，保留原工作区改动。

## 提交前审查

- 三路独立只读审查覆盖采集器、资金费与持仓、API 与面板，并对修正后的相关路径复审通过。
- 补充回归并修正：缺行情时原币合约入账、缺费率变零、休市补记、RWA 候选和反转判断、跨重开预测，以及 PopDEX 零数量顶档。

## 约束

保留原目录未提交修改；不改数据库结构、不升级依赖、不提交交易、不发真实通知。既有主线基线：Python 129 项、Node 21 项通过。

## 官方依据

- https://docs.txflow.com/api/info/marketTicker （资金费百分数、毫秒结算时间）
- https://docs.txflow.com/api/websocket/allAssetCtxs
- https://docs.qfex.com/api-reference/rest/market-data/contracts
- https://docs.qfex.com/qfex/funding
- https://docs.qfex.com/qfex/fees
- https://popdex.xyz/docs/common/quick-start
- https://api.popdex.xyz/api/v1/config/symbols
