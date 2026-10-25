# 第一期开通验收记录（时间点快照）

## 代码与自动测试

- PR：[#2](https://github.com/devanhaozhen-ai/lfm2-spot/pull/2)。实现提交 `a2f4af6`，之后仅有真实采集数据和本验收记录的更新。
- [Push CI](https://github.com/devanhaozhen-ai/lfm2-spot/actions/runs/40158958850) 和 [PR CI](https://github.com/devanhaozhen-ai/lfm2-spot/actions/runs/40158984973)：success。
- Python 74项（原53项全部保留）、Node 5项通过；Python编译、工作流YAML解析通过。
- 原 `validate_lfm3.py --allow-partial` 独立验算通过；原历史CVD仍如实partial，历史快照的读时stale未被掩盖。

## GitHub真实手动验收

[40158984924](https://github.com/devanhaozhen-ai/lfm2-spot/actions/runs/40158984924)，workflow_dispatch，分支 `feat/lfm32-rest-monitor`：collect 与 lfm32 两个job均success。

发布 request_id=`lfm32-acceptance-20261025`、collection_run_id=`40158984924` 与触发请求一致。`run_lfm32.py --ref feat/lfm32-rest-monitor` 固定提交读取与读时质量通过。Actions内独立验证器通过；源归档 `lfm32-source-40158984924` 上传成功（约1.18MB，保留30天）。当前云环境下载artifact的Azure重定向返回403，未在本地复验该份远端归档；本地另一轮真实归档已通过独立验证，不将二者混称。

快照数据（不是后续读取时的实时行情）：

| 交易所 / 币种 / 粒度 | 窗口 | 大单买入额 | 大单卖出额 | 净额 |
|---|---|---:|---:|---:|
| Binance / USDT / 聚合组 | 5m | 592944.0647355 | 0 | 592944.0647355 |
| Binance / USDT / 聚合组 | 15m | 972759.0231495 | 441928.1821898 | 530830.8409597 |
| Binance / USDT / 聚合组 | 60m | 3714307.6520147 | 966509.6082763 | 2747798.0437384 |
| Coinbase / USD / 单笔match | 5m、15m、60m | 0 | 0 | 0 |

这里的Coinbase 0是**完整覆盖中没有达到固定10万USD门槛的match**，不是用0代替缺失；动态分位大单另列。两所全部现货CVD方向存在冲突，大单方向尚未双所确认，不推断机构吸筹。两所订单簿快照可用，但连续吸收证据不可用。合约OI/费率/主动流接口451，金额不填充。

摘要示例：

```json
{
  "generated_utc": "2026-10-25T03:39:24.302000Z",
  "window_end_utc": "2026-10-25T03:35:00Z",
  "quality_status": "partial",
  "monitoring_eligible": true,
  "monitoring_scope": "verified fresh spot windows only",
  "full_evidence_eligible": false,
  "analysis": {
    "signals": [
      "信号冲突"
    ],
    "buy_evidence_score_0_100": null,
    "observed_evidence_points": 43.128,
    "available_weight": 75,
    "missing_dimensions": [
      "book_absorption",
      "derivatives"
    ],
    "predictive_use_eligible": false
  },
  "errors": {
    "futures_oi": "HTTP Error 451: ",
    "futures_funding": "HTTP Error 451: ",
    "futures_flow": "HTTP Error 451: "
  }
}
```

## 历史保留核验

以升级前 `4e5f0df` 对比手动运行后的分支：

| CSV | 原键数量 | 原完整记录 | 删除的旧键 | 变化/丢失的完整记录 |
|---|---:|---:|---:|---:|
| history.csv | 2510 | 2509 | 0 | 0 |
| price_history.csv | 13286 | 13286 | 0 | 0 |
| cvd_history.csv | 12264 | 7386 | 0 | 0 |

独立健康仍可能因原长期缺口失败；没有为使其变绿而移除门槛或历史告警。

## 自动调度状态与未完成能力

原五分钟schedule保留、代码中scheduled与manual_workflow_dispatch两种元数据测试通过。验收时最新实际schedule仍为 [40147157997](https://github.com/devanhaozhen-ai/lfm2-spot/actions/runs/40147157997)（升级前）；**新版本下一次真实schedule执行尚待观察**，手动成功不等于定时已验收。GitHub延迟/跳过调度的问题未被解决为SLA。

第二期常驻服务未部署；无连续盘口变动/真实撤单归因/冰山确认，无清算逐笔源；合约地域限制未解决。未回测或样本外验证，完整评分因缺项为null，不可用于预测。没有购买API或服务器；部署预算见 [第二期方案](LFM32_WEBSOCKET_PLAN.md)。
