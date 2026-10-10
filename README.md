# LFM 2.0 — Spot Confirmation Collector

Free GitHub Actions 5-minute REST-based BTC spot taker-flow collector. Python standard library only, no API keys, no local machine required.

公开仓库：https://github.com/devanhaozhen-ai/lfm2-spot

公开 JSON：https://raw.githubusercontent.com/devanhaozhen-ai/lfm2-spot/main/data/latest.json

手机查看运行状态：https://github.com/devanhaozhen-ai/lfm2-spot/actions

自动任务配置在 `.github/workflows/collect.yml`，每 5 分钟触发一次。GitHub 调度可能延迟；以 JSON 的 `generated_utc`、`latest_closed_5m_end_utc` 和 `coverage` 为准。

## Deployment
1. Create a **public** GitHub repository, e.g. `lfm2-spot`.
2. Upload the contents of this folder to the repository root, including `.github/workflows/collect.yml`.
3. In Settings → Actions → General → Workflow permissions select **Read and write permissions** (if available), and save.
4. In Actions → LFM2 spot collection → Run workflow, start the first run.
5. Check `data/latest.json` and Actions logs. Each run fetches the latest closed hour from the public APIs and reconstructs twelve five-minute intervals. The 15m and 60m windows can be complete on the first run. Missing data is explicitly marked incomplete.
6. Public machine-readable endpoint: `https://raw.githubusercontent.com/USERNAME/REPOSITORY/main/data/latest.json` (replace placeholders; branch may differ).

## Important limitations
- GitHub schedule runs can be delayed or skipped; this collector backfills the most recent hour on each run and explicitly marks 15m/60m windows incomplete when any five-minute interval is still missing. The collection is periodic, not a continuously connected trade feed.
- Binance closed 1-minute candlesticks expose **taker buy quote volume** and total quote volume, giving exact interval net taker flow without paginating trades. USDT is treated as USD approximation.
- Coinbase REST trades are paginated backward; capped at 80 pages per run. The window is `complete` only if pagination reaches a trade before its start. Coinbase `side` is the maker side, so it is inverted.
- `history.csv` retains all existing interval keys without a row-count cutoff, and repeated interval keys are deduplicated. Failed backfills never overwrite a previously complete interval.
- This is **5-minute aggregate CVD**, not tick-by-tick cumulative order flow. Missing windows are never silently filled.
- GitHub repository must be public if you want ChatGPT to read `raw.githubusercontent.com` without authentication. Public data only; never commit API secrets.
- No guaranteed minute-exact timing or uninterrupted collection. If collection fails, the JSON records `unavailable` and no false signal should be produced.
- If GitHub Actions' schedule becomes inactive after long repository inactivity, re-enable the workflow manually.

## Validation

The workflow checks maker/taker direction, missing-candle handling, and missing-interval behavior before collection. It commits the current output, then validates CVD, taker buy/sell ratios, timestamps, source-history sums, and the 3/3 and 12/12 interval coverage. An unavailable API or incomplete window makes the validation step fail while keeping the JSON status visible.

Run locally with `python collector.py`, then `python validate.py --require-complete`. No exchange account, API key, or trading permissions are used. This program only collects public BTC spot market data.

公式：`cvd_usd = buy_usd - sell_usd`；`taker_ratio = buy_usd / sell_usd`（主动卖出金额为零时为 null）。Coinbase 的 maker sell 记为主动买入，maker buy 记为主动卖出。15m 需要 3 个连续的完整 5m 区间，60m 需要 12 个；缺失时聚合指标为 null。

历史窗口按毫秒时间筛选：`end - minutes * 60000 <= start_ms < end`，包含起点、排除终点。筛选只决定聚合使用的记录；窗口外的历史仍保存在 `history.csv` 中。

## Data quality and currency compatibility

Existing `markets.binance`, `markets.coinbase`, `15m`, `60m`, `coverage`, `cvd_usd`, `taker_ratio`, and the CSV columns remain available. Binance `*_usd` names are legacy compatibility fields: their native quote unit is **USDT**, not converted USD. Coinbase uses **USD**. `market_units` declares this explicitly; cross-exchange notional aggregation is disabled until a dated FX conversion is supplied. Taker ratios are dimensionless.

The collector uses Decimal notionals, validates closed candle boundaries, checks Coinbase cursor progress, duplicate conflicts and trade-ID continuity, and requires the initial trade snapshot to cover the interval end. Gaps, truncation or stale snapshots are incomplete; partial metrics are null. The Coinbase trade-ID continuity check is conservative: a gap means coverage is unverified, rather than proving a particular trade was lost.

Successful historical intervals are immutable. CSV and JSON writes replace their prior files atomically, and there is no history row-count cutoff. Missing runs are backfilled from the last successful timestamp for up to six hours; Coinbase retains the bounded 80-page scan and reports incomplete if it cannot cover the required range. Network calls use 20-second timeouts, four attempts, Retry-After handling, structured exception logs, and a total seven-minute network deadline inside the ten-minute Actions job limit.

### Freshness must be evaluated when reading

`data_age_seconds` means seconds since `generated_utc`. `market_data_age_seconds` means seconds since `latest_closed_5m_end_utc`. `quality_status` is `ok`, `incomplete`, or `stale`; either age over 900 seconds is stale. `missing_intervals` lists missing five-minute starts for each exchange/window. `monitoring_eligible` is false for stale, incomplete, or failed-source observations.

**A static raw JSON cannot change after a stopped workflow.** Its quality fields are valid as of `quality_evaluated_utc`, not indefinitely. Consumers, including ChatGPT monitoring tasks, must recalculate age against the current UTC time on every read and reject stale observations. Use `python check_latest.py --require-usable` to fetch the public JSON and return refreshed quality fields, or import `quality.refresh_quality(data)`. This preserves the raw URL and existing fields without pretending that a stopped scheduler can update its own status.

GitHub's schedule is enabled every five minutes but is best-effort; queued runs can be delayed or dropped. The configured equivalent cron is `2-59/5 * * * *`. No minute-exact delivery is promised. If required freshness is not met, block directional monitoring, backfill on the next run, and inspect the Actions page. This repository collects spot flow only and does not itself establish derivatives short squeezes or submit orders.
