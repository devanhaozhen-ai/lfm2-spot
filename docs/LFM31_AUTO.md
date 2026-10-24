# LFM 3.1 自动采集与轻量读取

- 保留现有 LFM 2.0 / LFM 3.0 数据、CSV历史、手动 workflow_dispatch。
- `.github/workflows/collect.yml` 增加 GitHub Actions `*/5 * * * *` 定时采集（UTC）；实际启动可能延迟或漏跑，不保证精确5分钟。
- 每次采集运行 `collector.py`、`lfm3.py`、原有校验，再运行 `lfm3_summary.py`。
- 精简接口：`https://raw.githubusercontent.com/devanhaozhen-ai/lfm2-spot/main/data/lfm3_summary.json`
- 完整接口：`https://raw.githubusercontent.com/devanhaozhen-ai/lfm2-spot/main/data/lfm3_latest.json`
- 精简文件仅保留双所5m/1h最新收盘、EMA20/60、RSI14、成交量、15m/60m CVD、主动买卖比、历史覆盖率、请求ID、时间与错误。**不包含完整K线**。
- 读取端必须按当前UTC重新核算 `generated_utc`、`latest_closed_candle_utc` 年龄；超过900秒或窗口不完整时停止给出实时信号。构建时的 `monitoring_eligible_at_build` 不能代替读取时判定。
- Binance USDT与Coinbase USD不可相加；Coinbase七天CVD缺口不得用零填充；无OI、资金费率、ETF数据时不能声称衍生品/机构信号已确认。
- 定时采集对交易所API及GitHub Actions有资源消耗和额度限制。GitHub cron可能延迟；如需降低频率，可改为 `*/15 * * * *`。
- 工作流执行成功不代表普通聊天一定能访问GitHub。连接器或公开URL读取失败时，应报告失败，不把旧数据当作新数据。

验收：合并到main后查看 Actions 的自动运行、仓库内精简JSON更新时间、JSON字段与实时年龄。合并前定时触发不会在功能分支运行。
