# LFM3 Work manual collection, with LFM2 compatibility

This repository supports scheduled collection every five minutes (best effort) and manual GitHub Actions dispatch. The user authorized retaining scheduled collection for the LFM3.2 upgrade.

When asked **采集 BTC 数据**, use existing GitHub CLI authorization to execute `python work_collect.py`; wait for its actual result. It selects a new closed five-minute window, correlates the workflow and JSON through a unique request ID, waits for Actions success and public publication, preserves history, validates all windows, and independently recomputes original exchange data.

Only report success when the process exits zero and its report has `status=PASS`. Never treat old JSON as a fallback success. Recalculate UTC freshness if returning a saved report later. If authorization is missing, report that limitation and let the user complete official GitHub authorization without requesting passwords or tokens.

The same dispatcher now also verifies public `data/lfm3_latest.json`, seven days of 5m/1h candles, independent EMA/RSI calculations, historical CSV preservation, and direct exchange refetches. State its separate `lfm3.acceptance` result: unavailable historical Coinbase taker flow is PARTIAL, never a fabricated full seven-day CVD. Include LFM3 price/indicator coverage and its JSON URL alongside the existing Chinese CVD table. Reading static quality flags is insufficient; recalculate both ages using current UTC.

Return a Chinese table with Binance and Coinbase 15m/60m CVD, 15m/60m taker ratios, and completeness PASS/FAIL. Binance amounts are native USDT; Coinbase amounts are USD and must not be added without currency conversion. Also state Beijing generation time, source delay in minutes, actual Actions result/link and whether this observation is usable as LFM 2.0 spot input.

Keep the original JSON fields, successful history and existing computation formulas. Retain the existing five-minute scheduled collection and manual dispatch. Do not purchase paid infrastructure. See `docs/WORK_MANUAL_MODE.md` for the acceptance contract.


## 运行 LFM3.2

When the user says **运行 LFM3.2**, read main/data/lfm32_latest.json at a pinned latest commit and use lfm32.refresh_quality at current UTC (or execute `python run_lfm32.py`). If stale/incomplete, use existing GitHub authorization to execute `python run_lfm32.py --refresh`; wait for the correlated run and publication. A successful job may still have API gaps: report monitoring_eligible, monitoring_scope, per-venue freshness, errors, missing_dimensions and full_evidence_eligible separately. Never claim a full score when buy_evidence_score_0_100 is null. Never combine USDT and USD without a verified timestamped FX rate. Treat Binance aggregate groups and Coinbase individual matches as different granularities; no unique-order or institutional identity inference. REST book snapshots do not prove cancellations or absorption. Ask for a heatmap only if needed for the user's requested analysis, and inspect its source/time before combining it with current official observations. See docs/LFM32.md and docs/LFM32_WEBSOCKET_PLAN.md. Do not claim deployment of the second phase or predictive validation.


LFM3.2 follow-up: inspect `alternative_derivatives.okx` only as separately attributed OKX BTC-USDT-SWAP context; it never repairs Binance 451 fields or its full-score dimension. Use realizedRate for OKX settled funding and unit=0/BTC taker flow. `spot_only_evidence` is a separate per-venue experimental rubric; no pooled score, cross-venue score ranking or institutional-entry probability. Re-audit schedule with `lfm32_schedule.py`/reader at current UTC. An actual scheduled run does not establish five-minute continuity, and a legacy workflow failure must be reported separately from a successful lfm32 job. See docs/LFM32_FIXES.md.
