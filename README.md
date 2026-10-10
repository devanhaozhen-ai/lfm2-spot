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
- `history.csv` retains up to 12,000 rows, and repeated interval keys are deduplicated.
- This is **5-minute aggregate CVD**, not tick-by-tick cumulative order flow. Missing windows are never silently filled.
- GitHub repository must be public if you want ChatGPT to read `raw.githubusercontent.com` without authentication. Public data only; never commit API secrets.
- No guaranteed minute-exact timing or uninterrupted collection. If collection fails, the JSON records `unavailable` and no false signal should be produced.
- If GitHub Actions' schedule becomes inactive after long repository inactivity, re-enable the workflow manually.

## Validation

The workflow checks maker/taker direction, missing-candle handling, and missing-interval behavior before collection. It commits the current output, then validates CVD, taker buy/sell ratios, timestamps, source-history sums, and the 3/3 and 12/12 interval coverage. An unavailable API or incomplete window makes the validation step fail while keeping the JSON status visible.

Run locally with `python collector.py`, then `python validate.py --require-complete`. No exchange account, API key, or trading permissions are used. This program only collects public BTC spot market data.

公式：`cvd_usd = buy_usd - sell_usd`；`taker_ratio = buy_usd / sell_usd`（主动卖出金额为零时为 null）。Coinbase 的 maker sell 记为主动买入，maker buy 记为主动卖出。15m 需要 3 个连续的完整 5m 区间，60m 需要 12 个；缺失时聚合指标为 null。
