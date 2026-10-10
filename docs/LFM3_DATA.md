# LFM 3.0 手动数据契约

指令：**采集 BTC 数据**。仍通过 `work_collect.py` 与 `workflow_dispatch`，不会恢复 schedule。

## 文件与币种

- `latest.json` / `history.csv`：旧 LFM 2.0 结构、窗口、字段和成功历史保持兼容。
- `price_history.csv`：UTC 开始/结束、交易所、周期、OHLC、BTC 成交量、可用的真实成交额和 taker 字段。Coinbase 不提供的字段留空。
- `cvd_history.csv`：真实五分钟主动买卖额、净额、比值、覆盖状态及来源。成功记录不可改写；unavailable 可由真实数据升级。
- `lfm3_latest.json`：两个交易所各自七天闭合 5m/1h K线、指标、当前15m/60m流量、七天历史 CVD 覆盖、读取时质量。

Binance BTCUSDT 的金额为 USDT；Coinbase BTC-USD 为 USD。没有汇率换算，禁止直接相加。旧 `*_usd` 字段原名保留，实际币种通过 quote_currency 说明。

部分旧历史以原始买入、卖出、CVD分别四舍五入到分，三列算术可能相差0.01计价单位。保留这些原观察值，在新CSV逐行注明 rounding_policy；仅这类明确的旧记录允许一分舍入差，新建K线CVD必须由已舍入买入减卖出。超过容差仍失败，既不改旧历史也不扩大源数据复算容差。

## 时间、指标与历史

所有区间为 `[start_utc, end_utc)`，输出 ISO 8601 UTC。latest_closed_candle_utc 是五分钟闭合边界；每个周期各有 start/end 和最新 K线。七天分别为2016根5m、168根1h，小时边界向下取整。

EMA 使用 SMA 种子与递推平滑；RSI 使用 Wilder 14 期的均值种子与递推平滑。计算只使用到目标闭合边界的连续真实历史，记录实际计算起点与根数。后续运行累积历史会扩大计算窗口；不重置/删除已保存数据。独立验算使用加权求和公式，与采集器迭代算法不同。

成交量变化为最新 K线相对上一根，以及相对之前20根均量的百分比；零分母为 null。高低点参考为最近20根最低 low / 最高 high，并注明时间范围。没有把这些参考直接转换为买卖信号。

Binance 历史 CVD = taker buy quote − (quote volume − taker buy quote)，逐根闭合 K线重建并独立再取源复算。Coinbase candles 不能重建方向；原采集器经过分页、重复和 maker/taker 校验的成交历史才可保留为 complete。无法取得整段真实成交则 unavailable，金额和比值为 null，不能按价格涨跌填充。七天不完整时 net_cvd_quote=null，当前15m/60m独立验收。

## 新鲜度与部分失败

静态 JSON 不能自动变旧。读取时必须调用 `refresh_lfm3_quality(data, current_utc)`，或等价地重新计算 generated_utc 与 latest_closed_candle_utc 年龄，超过900秒则 stale 并关闭 monitoring_eligible。各市场、各周期还检查最新已闭合 K线与当前应有闭合边界的差。1h 原始年龄正常可达3600秒，应检查它是否缺少最新已闭合小时，而非机械套用十五分钟原始年龄。

API 失败记录到 errors，其他交易所的有效结果仍持久化；无法采集不等于空交易量。Actions 发布部分结果后校验步骤会失败，Work 明确返回失败，不把旧成功 JSON 当成新采集。全局监测要求两所价格、当前15m/60m和时间有效；历史 Coinbase unavailable 单独记录 historical_cvd_quality=partial。

Work 将本次请求编号、run ID、commit 固定快照与两个公开 JSON 核对；每次重新取交易所七天 OHLCV及Binance主动量验证，全量指标独立复算，CSV旧成功记录不得丢失或改变。

采集成功为 report.status=PASS，历史 CVD 完整能力为 report.lfm3.acceptance=PASS/PARTIAL。Coinbase 七天 CVD unavailable 时最终能力验收必须 PARTIAL，即使当前实时价格与CVD均可用。
