# LFM3.2 快速入口

输入 **“运行 LFM3.2”**：在有仓库与GitHub授权的Work环境执行 `python run_lfm32.py`，过期或不完整时 `python run_lfm32.py --refresh`。如实报告缺项，再结合带来源/时间的用户清算热力图分析；不保证普通新聊天自动记住本仓库。

- [最新 LFM3.2 JSON](https://raw.githubusercontent.com/devanhaozhen-ai/lfm2-spot/main/data/lfm32_latest.json) / [紧凑摘要](https://raw.githubusercontent.com/devanhaozhen-ai/lfm2-spot/main/data/lfm32_summary.json)
- [第一期模块、来源、限制与测试](docs/LFM32.md) · [系统审计与风险](docs/LFM32_AUDIT.md) · [第二期WebSocket方案与费用预算](docs/LFM32_WEBSOCKET_PLAN.md)

第一期为有覆盖证明的REST窗口，**不是完整实时逐笔监控**。缺失数据为null；金额保留USDT/USD及交易所来源。订单簿仅快照，吸收/撤单不可用；完整0–100分在缺项时为null，评分未经回测不得用于预测。下方保留原系统的数据契约与使用说明。

# BTC LFM 3.2 — 官方 REST 资金行为证据（兼容 LFM2 / LFM3.1）

仓库：https://github.com/devanhaozhen-ai/lfm2-spot

公开数据：https://raw.githubusercontent.com/devanhaozhen-ai/lfm2-spot/main/data/latest.json

LFM 3.0 接口：https://raw.githubusercontent.com/devanhaozhen-ai/lfm2-spot/main/data/lfm3_latest.json

输入 **“采集 BTC 数据”**，同一次手动运行同时更新 LFM 2.0 和 LFM 3.0。LFM 3.0 包含两所七天闭合 K 线、EMA20/60、Wilder RSI14、成交量变化、高低点参考位及真实 CVD 覆盖信息。同时保留 GitHub 五分钟 cron（best effort，可能显著延迟），和手动采集入口。

七天 K 线持久化于 `data/price_history.csv`；5m 每所 2016 根，1h 每所 168 根，均使用 UTC 半开区间。完整旧记录不可覆盖，历史不会因滚动窗口删除；每次重新获取七天数据、去重并补缺。JSON 的 `markets.<exchange>.5m/1h.candles` 包含整个七天窗口，长期历史保存在 CSV。

`data/cvd_history.csv` 是每个五分钟区间的净主动买卖额，不是跨缺口累加的 CVD。Binance 使用真实 K 线 quote volume（字段7）和 taker buy quote volume（字段10），卖出=总额−买入。Coinbase 只用已完整分页的真实成交重建；七天中没有可靠成交记录的区间为 `unavailable`，金额和比值为 null/空。不会以价格涨跌猜测方向；七天 CVD 不完整时总净额为 null。历史 CVD 的 PARTIAL 与当前七天价格、当前15m/60m完整性分别报告。

EMA 以首次连续序列前 N 个收盘价均值为种子，alpha=2/(N+1)；RSI 使用 Wilder 14 期，平盘序列=50。每项指标注明实际计算起点、终点、根数、币种和来源；缺口前后的数据不混合计算。高低点取最近20根 K 线，仅为价格参考。

读取时执行 `refresh_lfm3_quality` 或重新按当前 UTC 计算年龄。generated_utc 与最新五分钟数据年龄不得超过900秒；1h K线按相对于最近整点闭合边界的落后时间判断，不把正常的小时内年龄误判为超时。七天补采成功不能代替实时新鲜度。详见 [LFM 3.0 数据契约](docs/LFM3_DATA.md)。

在当前 ChatGPT Work 任务中输入 **“采集 BTC 数据”**。Work 会复用已有 GitHub 授权，手动触发采集，等待 Actions 成功及公开 JSON 更新，完成验收后返回两所的 15m/60m CVD、主动买卖比和完整性。随后回普通聊天输入 **“运行 LFM 2.0”**，使用此接口作为最新现货数据输入。

采集工作流同时支持 schedule 和 workflow_dispatch，明确记录触发类型；健康工作流保留手动入口和采集完成后的 workflow_run 检查。GitHub调度不保证准时或连续，超过15分钟的数据不能用于实时信号。

同一个五分钟区间内重复请求时，Work 会等待下一根区间真正闭合，确保窗口推进。已有授权有效时无需每次登录；授权失效时由用户完成 GitHub 官方授权，不要求密码或访问令牌。

## 每次验收

- 本次独有请求编号必须匹配 GitHub 运行及 JSON，Actions 结果必须为 success。
- generated_utc 必须晚于触发前和本次触发时间；原公开地址必须已更新为本次请求。
- latest_closed_5m_end_utc 必须推进，并等于验收时最近闭合的五分钟边界。
- 用读取时实际 UTC 重新计算两个年龄，任一超过 900 秒即 stale；不能只信任静态 quality_status。
- 两所 15m 为 3/3、60m 为 12/12，全部 coverage=complete、errors 为空；金额、CVD、买卖比及历史聚合有效。
- 从原始交易所 REST API 独立复算最近一小时，核对 Coinbase maker/taker 方向、分页、重复成交及成交 ID 连续性。
- 任一步失败返回明确错误，关闭监测资格，不使用旧 JSON 宣称本次采集成功。

执行器为 work_collect.py；详细契约见 [Work 手动模式](docs/WORK_MANUAL_MODE.md)。GitHub 原有 Run workflow 按钮仍可使用，当前 Work 指令会自动完成触发和验收。

## 金额、窗口及历史

cvd_usd = buy_usd - sell_usd；taker_ratio = buy_usd / sell_usd，卖出金额为零时比值为 null。Coinbase REST side 是 maker 方向：maker sell 计为主动买入，maker buy 计为主动卖出。

Binance BTCUSDT 的旧 *_usd 字段实际单位为 **USDT**；Coinbase BTC-USD 为 **USD**。market_units 明确单位，未做币种换算，两所金额不能直接相加。CVD 为对应窗口的净主动成交额。

15m/60m 只使用 [end - minutes × 60000, end) 内完整的五分钟记录，分别要求 3 和 12 个区间。原有 markets.binance、markets.coinbase、15m、60m、coverage、cvd_usd、taker_ratio 和 CSV 列保持兼容。新增 collection_mode、request_id、collection_run_id 便于核对手动请求，不修改旧字段名称。

history.csv 保留全部旧键；完整记录重采后不覆盖，数值时间戳去重，失败写入保持原有效文件，缺失记录可升级为完整。每轮最多补采六小时，Coinbase 上限 200 页，并保留全局网络超时、重试、分页连续性检查。仍达到上限或无法覆盖完整区间时必须 incomplete；不会清除采集错误来冒充成功。

手动模式不承诺两次请求之间的全天连续历史。history_quality 如实报告历史缺口；当前窗口之外的停采缺口不阻止完整、新鲜的最近一小时观察用于分析。当前 15m/60m 内的缺口仍关闭监测资格。已有历史不会因改造或重新部署丢失。

## 读取与测试

普通聊天分析前必须重新读取公开 JSON，并按实际 UTC 计算 generated_utc 和 latest_closed_5m_end_utc 的年龄。过期或不完整时，回 Work 输入“采集 BTC 数据”。现货数据可作为 LFM 2.0 输入，判断轧空还需其他相关证据。

只采集公开行情，不需要交易所密钥，不下单，不创建付费资源。网络超时、有限重试、异常日志和原子写入机制保留。data/health.json 是采集后的静态健康记录，也需检查其时间。

测试：python -m unittest discover -s tests；备用读取器测试：node --test tests/external_health.test.mjs。external-health 中以前准备的外部读取器未部署，不是当前手动采集入口。
