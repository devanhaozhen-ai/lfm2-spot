# LFM3 Work manual collection, with LFM2 compatibility

This repository uses manual GitHub Actions dispatch, not scheduled collection.

When asked **采集 BTC 数据**, use existing GitHub CLI authorization to execute `python work_collect.py`; wait for its actual result. It selects a new closed five-minute window, correlates the workflow and JSON through a unique request ID, waits for Actions success and public publication, preserves history, validates all windows, and independently recomputes original exchange data.

Only report success when the process exits zero and its report has `status=PASS`. Never treat old JSON as a fallback success. Recalculate UTC freshness if returning a saved report later. If authorization is missing, report that limitation and let the user complete official GitHub authorization without requesting passwords or tokens.

The same dispatcher now also verifies public `data/lfm3_latest.json`, seven days of 5m/1h candles, independent EMA/RSI calculations, historical CSV preservation, and direct exchange refetches. State its separate `lfm3.acceptance` result: unavailable historical Coinbase taker flow is PARTIAL, never a fabricated full seven-day CVD. Include LFM3 price/indicator coverage and its JSON URL alongside the existing Chinese CVD table. Reading static quality flags is insufficient; recalculate both ages using current UTC.

Return a Chinese table with Binance and Coinbase 15m/60m CVD, 15m/60m taker ratios, and completeness PASS/FAIL. Binance amounts are native USDT; Coinbase amounts are USD and must not be added without currency conversion. Also state Beijing generation time, source delay in minutes, actual Actions result/link and whether this observation is usable as LFM 2.0 spot input.

Keep the original JSON fields, successful history and existing computation formulas. Do not add scheduled collection or paid infrastructure. See `docs/WORK_MANUAL_MODE.md` for the acceptance contract.
