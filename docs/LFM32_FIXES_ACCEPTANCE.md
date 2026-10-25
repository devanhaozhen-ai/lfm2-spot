# LFM3.2 修复验收记录（非实时快照）

PR：[#3](https://github.com/devanhaozhen-ai/lfm2-spot/pull/3)，实现提交008dfd9。

- [Push CI](https://github.com/devanhaozhen-ai/lfm2-spot/actions/runs/40172534881)：success。
- [PR CI](https://github.com/devanhaozhen-ai/lfm2-spot/actions/runs/40172560300)：success。
- [实际分支手动采集](https://github.com/devanhaozhen-ai/lfm2-spot/actions/runs/40172560275)：原collect与lfm32均success；LFM3.2独立源归档校验、实际事件审计、原LFM3完整验证均success。
- Python单元/回归测试96项通过、Node读取器测试5项通过，失败0项。Python编译、YAML解析、diff空白检查通过。
- 本地真实源归档 `/tmp/lfm32-fix-live` 的独立LFM3.2校验通过；远端Actions中校验通过。现货完整可用与完整评分证据齐全是两个不同结论。
- OKX三个官方组件在GitHub Actions环境中也实际可用；Binance三个451错误仍保留。不会把另一个交易所的数据写成Binance。
- lfm3.py、quality.py、validate_lfm3.py、lfm3_summary.py相对修复前逐字节不变；原collector仅增加no-cache请求头，公式/历史/验证门槛不变。

旧CSV保留核对结果（基线fe94902）：

```json
[
  {
    "file": "history.csv",
    "old_rows": 2622,
    "old_complete": 2603,
    "removed_keys": 0,
    "changed_complete": 0
  },
  {
    "file": "price_history.csv",
    "old_rows": 13408,
    "old_complete": 13408,
    "removed_keys": 0,
    "changed_complete": 0
  },
  {
    "file": "cvd_history.csv",
    "old_rows": 12376,
    "old_complete": 7480,
    "removed_keys": 0,
    "changed_complete": 0
  }
]
```

本次实际数据摘要：

```json
{
  "generated_utc": "2026-10-25T07:23:32.592000Z",
  "window_end_utc": "2026-10-25T07:20:00Z",
  "quality_status": "partial",
  "monitoring_eligible": true,
  "full_buy_evidence_score_0_100": null,
  "full_missing_dimensions": [
    "spot_large_flow",
    "book_absorption",
    "derivatives"
  ],
  "spot_scores": {
    "binance": {
      "score_0_100": 64.278,
      "available_weight": 100,
      "missing_dimensions": []
    },
    "coinbase": {
      "score_0_100": 58.686,
      "available_weight": 100,
      "missing_dimensions": []
    }
  },
  "okx_components": {
    "oi": {
      "status": "available",
      "as_of_utc": "2026-10-25T07:20:00Z"
    },
    "funding": {
      "status": "available",
      "actual_funding_utc": "2026-10-25T00:00:00Z",
      "rate": "0.0000218947520699"
    },
    "flow": {
      "status": "available",
      "volume_currency": "BTC"
    }
  },
  "continuous_5m_verified": false,
  "publication_actual_event": "workflow_dispatch",
  "errors": {
    "futures_oi": "HTTP Error 451: ",
    "futures_funding": "HTTP Error 451: ",
    "futures_flow": "HTTP Error 451: "
  }
}
```

现货分数是逐所的实验性描述刻度，不是机构概率，也不用于两所排名。此处数值是上述时间快照，未来读取必须重算900秒新鲜度。

仍未通过的验收/外部故障：

1. 持续五分钟scheduled采集：不通过。最近真实schedule为40164526017，lfm32成功、原collect因Coinbase覆盖失败；此后到本次核查没有新的schedule，最大观察间隔16589秒。本次手动成功不能替代定时验收。
2. Binance OI/资金费率/主动流：HTTP451，未绕过或伪造。
3. 连续盘口吸收、唯一主动订单/机构识别、评分历史回测和样本外检验：没有实现或完成；相应资格/评分继续关闭。

未购买或开通付费服务。主分支合并后的最新运行以Actions与data/lfm32_latest.json为准；本文件保留该次分支验收事实。
