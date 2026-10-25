# LFM3.2 第一次验收后的修复

## 1. HTTP 451 与独立替代市场

Binance `fapi.binance.com` 的OI、费率和合约成交仍返回451。采集器不使用镜像域名、VPN、地区切换或代请求去绕过限制，原 `derivatives` 字段保持 unavailable。Bybit官方公开接口实测403；Deribit/Kraken公开数据可达，但本次仅集成语义已核验且同为BTC线性永续的 **OKX BTC-USDT-SWAP**，避免增加不必要的数据源和不同合约单位。

新增 `alternative_derivatives.okx.components`，完全独立于Binance：

| 数据 | 官方API | 字段及单位 |
|---|---|---|
| OI历史 | `/api/v5/rubik/stat/contracts/open-interest-history` | 5m；数组依次为时间、合约张数、币数量、USD名义价值。使用币数量作为BTC OI，USD名义价值另标币种，不当成USDT。|
| 资金费率历史 | `/api/v5/public/funding-rate-history` | 使用 **realizedRate（实际结算）** 和 fundingTime；fundingRate是预测值，不冒充实际费率。|
| 合约主动成交量 | `/api/v5/rubik/stat/taker-volume-contract` | 指定instId、period=5m、**unit=0（BTC）**；数组顺序为时间、主动卖、主动买。CVD单位BTC，不估算成USDT名义额。|

官方说明：[OKX API文档](https://www.okx.com/docs-v5/en/#rest-api-trading-data)。历史接口每次至多100条，最近一小时只需要12个5m区间；使用一页并验证每个所需时间键，不够则null。当前未闭合5m区间排除；OI变化要求准确端点，不插值。费率历史最多约三个月；OI/主动量历史上限约1440条，实际以官方当时保留范围为准。

OI/费率/主动流分别限时、失败隔离、请求审计、压缩原始归档及独立验证。所有响应标交易所、合约、来源、事件/观测时间。替代数据**不回填Binance、不改变Binance缺项、不补足完整评分的合约维度**。它是另一个市场的辅助背景，不能用于声称“Binance OI 已恢复”。未购买API。

## 2. 完整评分保持null；现货单独评分

原完整权重保留，但跨所大单维度进一步收紧：Binance聚合组与Coinbase单笔match不能建立可比的“大额单笔”共同口径，因此完整 `spot_large_flow` 维度也保持不可用；方向一致性维度改看**全部主动资金流方向**。订单簿吸收和原Binance合约维度仍分别缺失。不会把这些维度的缺项当0或重新缩放原75分。

新增 `spot_only_evidence`，属于**独立、实验性的逐交易所现货买入证据刻度**：

| 维度 | 权重 | 计算 |
|---|---:|---|
| 全部主动资金流 | 50 | 本所15m主动买额 /（主动买额+主动卖额），等价于净流不平衡映射到0–1 |
| 本所相对大额记录 | 25 | 本所60m经验分位门槛之上、15m记录中的主动买额占比；明确记录粒度 |
| 价量配合 | 25 | 15m价格方向配合末5m BTC量相对前两根均值；上涨放量=1、下跌放量=0、其他=0.5 |

每所三个维度都有效且5/15/60m数据新鲜完整，才输出该所0–100分。动态样本不足、无合格记录导致比值未定义或任意关键数据过期，完整现货分为null，同时保留有效维度、观察分和可用权重。

**不合并两所现货分，不把分数差当成强弱排名，不声称它是机构进场概率。** 同一分位数也不能消除聚合与单笔差异。分位阈值为同窗口描述性校准，尚未回测或样本外检验；predictive_use_eligible=false。摘要保留重算该分所需的15m动态数据，避免读取摘要时丢失维度。

## 3. 调度事实与修复边界

核查时，最新LFM3.2 JSON并非手动：collection_run_id=40164526017、collection_mode=scheduled、生成2026-10-25 05:10:23.643Z。

[真实schedule运行](https://github.com/devanhaozhen-ai/lfm2-spot/actions/runs/40164526017)开始05:09:55Z；lfm32 job success，原collect job在独立LFM3可用性验证失败（Coinbase snapshot does not cover interval end）。保留失败，不放宽质量规则。原HTTP请求增加Cache-Control: no-cache以减少缓存快照问题，但不保证服务端缓存消失，也不将取不到结束边界视作完整。

07:19:56Z审计：最近24h有7次实际schedule；观察到间隔6453–16589秒（约108–276分钟），最新schedule距读取约130分钟，**不满足持续五分钟运行**。工作流是active、已有cron配置；没有证据表明把active再enable一次会解决调度延迟，因此不冒充已修好GitHub平台调度。

新增 `lfm32_schedule.py`：读取GitHub官方运行、event、job结果；比对发布run_id及模式；计算运行间隔、最近触发年龄和数据年龄。发布 `data/lfm32_schedule.json`，并在 `run_lfm32.py` 每次读取时重新审计，不能信任历史静态“正常”。手动触发不计入schedule次数；整体workflow失败与lfm32 job成功分别显示。

连续性验收要求最近24h至少3次真实schedule、间隔不超过600秒容差、最新触发不超过900秒，且最新schedule的lfm32 job成功。否则continuous_5m_verified=false；存在旧间隔将继续显示degraded。该审计使用有界最近100次main运行，报告覆盖限制，无法证明API列表之外的完整运行史。正在运行的schedule job不冒充完成成功。

这是监测与诚实降级，**不是**独立常驻调度器，也没有五分钟SLA。严格持续采集仍需第二期常驻服务或用户授权的独立调度平台；本次未开通或购买。

## 4. 明确交易口径

保留旧trade_count/large_count以兼容，新增：

- record_count：源记录数；Binance聚合组、Coinbase match。
- underlying_match_count：Binance按每组 `l-f+1` 统计底层撮合笔数，Coinbase等于记录数。不能据此知道各撮合金额。
- large_record_count：超过门槛的源记录数。
- large_individual_match_count：Binance为null，Coinbase为真实单笔大单数。
- unique_taker_order_count：均为null；不拼出不存在的主动订单ID。
- large_amount_basis / statistical_contract：明确聚合组金额或单笔match金额、跨所大单记录不可比、机构身份未知。

一个聚合组金额达到10万，不证明该组内某一笔撮合达到10万；连续多组也不证明多个独立机构订单。动态阈值逐所计算，不用同分位数伪装同口径。

## 5. 验证与运行

```bash
python -m unittest discover -s tests
node --test tests/external_health.test.mjs
python lfm32.py --data-dir /tmp/lfm32-check
python validate_lfm32.py --data-dir /tmp/lfm32-check --require-usable
python validate_lfm3.py --allow-partial
python lfm32_schedule.py
python lfm32_schedule.py --require-continuous  # 现状会失败，属于真实调度问题
```

本地96项Python测试、5项Node测试通过，失败0；真实新归档的LFM3.2独立校验通过，OI/费率/主动量均取得OKX真实响应；原LFM3独立算术及覆盖校验通过，其旧数据读时stale和历史partial如实保留。

新增测试覆盖OKX实际/预测费率区分、OI张数/BTC/USD单位、主动买卖列顺序、当前未闭合柱排除、缺口null、错误/重复响应、替代源不污染Binance、现货单独评分缺项/过期、摘要可重算、聚合组不等于单笔大单，以及scheduled/manual分辨、稀疏调度、job与workflow结果分离。实际GitHub验收结果另见PR与Actions链接。
