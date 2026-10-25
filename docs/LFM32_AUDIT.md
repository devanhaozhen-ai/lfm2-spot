# 升级前审计（2026-10-25 UTC）

审计起点主分支 `4e5f0df`；检查 collector.py、lfm3.py、quality.py、validate_lfm3.py、lfm3_summary.py、work_collect.py、healthcheck.py 及两个原 Actions 工作流。未删除或修改历史观测。

## 发现及证据

| 项目 | 事实 / 风险 | 处理 |
|---|---|---|
| 调度配置 | collect.yml 已有 `2-59/5 * * * *`，README/AGENTS却称仅手动 | 按本次用户要求保留现有五分钟配置，纠正文档；区分实际event |
| 最近自动运行 | [40147157997](https://github.com/devanhaozhen-ai/lfm2-spot/actions/runs/40147157997)，schedule，2026-10-25 00:33:26Z，success；此前22:45:53、20:55:13、18:18:08，间隔显著超过5m | GitHub cron 非可靠常驻流；读时900秒门槛不放宽 |
| 最近手动运行 | [40113133030](https://github.com/devanhaozhen-ai/lfm2-spot/actions/runs/40113133030)，workflow_dispatch，2026-10-24 16:46:49Z，success；16:43:29亦成功，更早16:17:43失败 | 手动成功不能证明自动调度正常 |
| 独立健康 | [40147193785](https://github.com/devanhaozhen-ai/lfm2-spot/actions/runs/40147193785)，workflow_run，failure；health.json 两所最新15/60m缺口均为空 | 失败主要来自原LFM2长期历史门槛，不等于本轮API取数全失败；保留独立健康，不抹掉告警 |
| 长期缺口 | health.json Binance missing_count=2931、Coinbase=2932，均已超出6小时回补范围 | 保留旧CSV、历史告警；不能伪造填0或声称完整历史；新模块独立报告覆盖 |
| 旧LFM3数据 | 两所各2016根5m、168根1h价格通过独立算术核验；历史CVD partial；采集停顿后读时 stale | 原独立验证、EMA/RSI和缺口政策不变 |
| API可达性 | 云环境初测Coinbase默认请求403，使用正式采集器请求随后成功；Binance现货可达，fapi三个接口451 | 保存实际每次HTTP结果，隔离缺项；不把初次403当永久故障，也不掩盖合约限制 |
| 分页与预算 | 原Coinbase上限200页、全局420秒；新模块120页/每所100秒。高波动可能超预算 | 预算/页限到达即 unavailable，ID和时间覆盖双重核验 |
| 读时质量 | 原静态JSON在无新运行时不能自行衰老；原health是采集后触发，与GitHub调度同依赖 | 保留原质量机制，新读器独立重算；常驻服务/独立告警属第二期 |
| 手动执行器 | work_collect.find_run寻找旧标题 `BTC manual collection`，现workflow标题为`BTC collection` | 小范围兼容两种标题，防止手动请求关联超时 |
| 机构判定 | REST无法识别机构、主动订单全生命周期或盘口撤单原因 | 不提供机构名称/概率；分组粒度和不可用能力明确暴露 |

## 风险边界

REST 每轮重抓最近一小时可以覆盖短调度停顿；更长停顿在新历史文件中仍是缺失，没有凭空补写的记录。两所网络故障、同一IP限流、地域限制及Github调度延迟可能反复出现。没有付费API和下单功能，没有购买或申请服务器。没有回测成绩；评分禁用于预测。

本地基线53项 Python测试全部通过。升级后测试及实际Actions结果见 PR 和 Actions；文档中的时间/数据是审计快照，不是当前实时行情。
