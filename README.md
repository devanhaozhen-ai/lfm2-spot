# BTC LFM 2.0 — Work 手动采集

仓库：https://github.com/devanhaozhen-ai/lfm2-spot

公开数据：https://raw.githubusercontent.com/devanhaozhen-ai/lfm2-spot/main/data/latest.json

在当前 ChatGPT Work 任务中输入 **“采集 BTC 数据”**。Work 会复用已有 GitHub 授权，手动触发采集，等待 Actions 成功及公开 JSON 更新，完成验收后返回两所的 15m/60m CVD、主动买卖比和完整性。随后回普通聊天输入 **“运行 LFM 2.0”**，使用此接口作为最新现货数据输入。

采集工作流仅保留 workflow_dispatch，已移除定时采集。健康工作流保留手动入口和采集完成后的 workflow_run 检查，也没有定时计划。两次手动请求之间数据不会自动刷新；超过 15 分钟的数据不能用于实时信号。

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

history.csv 保留全部旧键；完整记录重采后不覆盖，数值时间戳去重，失败写入保持原有效文件，缺失记录可升级为完整。每轮最多补采六小时，Coinbase 上限 80 页，当前窗口无法完整覆盖时必须 incomplete。

手动模式不承诺两次请求之间的全天连续历史。history_quality 如实报告历史缺口；当前窗口之外的停采缺口不阻止完整、新鲜的最近一小时观察用于分析。当前 15m/60m 内的缺口仍关闭监测资格。已有历史不会因改造或重新部署丢失。

## 读取与测试

普通聊天分析前必须重新读取公开 JSON，并按实际 UTC 计算 generated_utc 和 latest_closed_5m_end_utc 的年龄。过期或不完整时，回 Work 输入“采集 BTC 数据”。现货数据可作为 LFM 2.0 输入，判断轧空还需其他相关证据。

只采集公开行情，不需要交易所密钥，不下单，不创建付费资源。网络超时、有限重试、异常日志和原子写入机制保留。data/health.json 是采集后的静态健康记录，也需检查其时间。

测试：python -m unittest discover -s tests；备用读取器测试：node --test tests/external_health.test.mjs。external-health 中以前准备的外部读取器未部署，不是当前手动采集入口。
