"""Publish a small, explicitly timestamped LFM 3.1 observation for chat readers.

Never infer missing taker flow or silently substitute old observations.
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from lfm3 import refresh_lfm3_quality


def summarize(source, checked_at=None):
    checked_at = checked_at or datetime.now(timezone.utc)
    data = refresh_lfm3_quality(source, checked_at)
    result = {
        "schema_version": "3.1-summary",
        "source_schema_version": source.get("schema_version"),
        "generated_utc": source["generated_utc"],
        "latest_closed_candle_utc": source["latest_closed_candle_utc"],
        "summary_built_utc": checked_at.isoformat().replace("+00:00", "Z"),
        "collection_mode": source.get("collection_mode"),
        "request_id": source.get("request_id"),
        "collection_run_id": source.get("collection_run_id"),
        "quality_status_at_build": data["quality_status"],
        "monitoring_eligible_at_build": data["monitoring_eligible"],
        "historical_cvd_quality": data["historical_cvd_quality"],
        "legacy_history_quality": {k: v for k, v in source.get("legacy_history_quality", {}).items() if k != "history_quality"},
        "legacy_historical_gaps": {exchange: {"missing_count": row.get("missing_count"), "outside_backfill_horizon": row.get("outside_backfill_horizon")} for exchange, row in source.get("legacy_history_quality", {}).get("history_quality", {}).items()},
        "stale_after_seconds": 900,
        "errors": data.get("errors", {}),
        "markets": {},
        "notes": [
            "Recalculate age from generated_utc and latest_closed_candle_utc at READ time; build-time quality is not live quality.",
            "Binance quote amounts are USDT; Coinbase quote amounts are USD. Never add them.",
            "CVD indicates taker buy minus taker sell, not institutional identity.",
            "Historical Coinbase CVD may be partial; missing values are not zero."
        ]
    }
    for name in ("binance", "coinbase"):
        market = data["markets"][name]
        frames = {}
        for frame in ("5m", "1h"):
            f = market[frame]
            candle = f.get("latest_candle")
            indicator = f.get("indicators")
            frames[frame] = {
                "coverage": f.get("coverage"),
                "intervals": f.get("intervals"),
                "expected": f.get("expected"),
                "fresh_at_build": f.get("fresh"),
                "latest_candle_end_utc": candle.get("end_utc") if candle else None,
                "close": candle.get("close") if candle else None,
                "volume_btc": candle.get("volume_btc") if candle else None,
                "ema20": indicator.get("ema20") if indicator else None,
                "ema60": indicator.get("ema60") if indicator else None,
                "rsi14": indicator.get("rsi14") if indicator else None,
                "volume_vs_prior20_pct": indicator.get("volume_vs_prior20_pct") if indicator else None,
                "support_reference": indicator.get("support_reference") if indicator else None,
                "resistance_reference": indicator.get("resistance_reference") if indicator else None,
            }
        flows = {}
        for frame in ("15m", "60m"):
            item = market["cvd"][frame]
            flows[frame] = {
                "coverage": item.get("coverage"),
                "intervals": item.get("intervals"),
                "expected": item.get("expected"),
                "buy_quote": item.get("buy_usd"),
                "sell_quote": item.get("sell_usd"),
                "cvd_quote": item.get("cvd_usd"),
                "taker_ratio": item.get("taker_ratio"),
            }
        hist = market.get("historical_cvd", {})
        result["markets"][name] = {
            "symbol": market.get("symbol"),
            "quote_currency": market.get("quote_currency"),
            "quality_status_at_build": market.get("quality_status"),
            "5m": frames["5m"],
            "1h": frames["1h"],
            "cvd": flows,
            "historical_cvd": {
                "coverage": hist.get("coverage"),
                "available": hist.get("available"),
                "expected": hist.get("expected"),
                "net_cvd_quote": hist.get("net_cvd_quote"),
            }
        }
    return result


def main():
    source = Path(sys.argv[1] if len(sys.argv) > 1 else "data/lfm3_latest.json")
    target = Path(sys.argv[2] if len(sys.argv) > 2 else "data/lfm3_summary.json")
    data = json.loads(source.read_text(encoding="utf-8"))
    result = summarize(data)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n", encoding="utf-8")
    print(f"LFM3 summary published: {target} ({target.stat().st_size} bytes), quality={result['quality_status_at_build']}")


if __name__ == "__main__":
    main()
