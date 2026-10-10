"""Independent arithmetic, CSV integrity and read-time LFM3 contract checks."""
import argparse
import csv
import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from quality import parse_time
from lfm3 import refresh_lfm3_quality

STEPS = {'5m': 300, '1h': 3600}


def close(actual, expected, label, tolerance=1e-7):
    if actual is None or not math.isfinite(float(actual)) or not math.isclose(float(actual), float(expected), rel_tol=1e-10, abs_tol=tolerance):
        raise ValueError(label + ' independent calculation mismatch')


def reference_ema(values, period):
    """Closed-form weighted sum, independent of collector's iterative EMA."""
    decay = 1 - 2 / (period + 1)
    tail = values[period:]
    return sum(values[:period]) / period * decay ** len(tail) + (1 - decay) * sum(value * decay ** (len(tail) - i - 1) for i, value in enumerate(tail))


def reference_rsi(values):
    changes = [b - a for a, b in zip(values, values[1:])]
    decay = 13 / 14
    tail = changes[14:]
    def average(sign):
        initial = sum(max(sign * v, 0) for v in changes[:14]) / 14
        return initial * decay ** len(tail) + sum(max(sign * v, 0) * decay ** (len(tail) - i - 1) for i, v in enumerate(tail)) / 14
    gain, loss = average(1), average(-1)
    return 50 if gain == loss == 0 else 100 if loss == 0 else 100 - 100 / (1 + gain / loss)


def load_rows(path):
    with path.open(newline='') as handle:
        return list(csv.DictReader(handle))


def validate(directory, require_usable=True, clock=None):
    directory = Path(directory)
    data = json.loads((directory / 'lfm3_latest.json').read_text())
    legacy = json.loads((directory / 'latest.json').read_text())
    clock = clock or datetime.now(timezone.utc)
    if data['schema_version'] != '3.0' or data['collection_mode'] != 'manual_workflow_dispatch':
        raise ValueError('LFM3 schema or manual mode invalid')
    if data['request_id'] != legacy['request_id'] or data['collection_run_id'] != legacy['collection_run_id']:
        raise ValueError('LFM3 and legacy output belong to different requests')
    if data['latest_closed_candle_utc'] != legacy['latest_closed_5m_end_utc'] or data['lfm2_generated_utc'] != legacy['generated_utc']:
        raise ValueError('legacy/live CVD timestamps differ')
    prices = load_rows(directory / 'price_history.csv')
    price_index = {}
    for row in prices:
        ident = (row['exchange'], row['timeframe'], parse_time(row['start_utc']).timestamp())
        if ident in price_index:
            raise ValueError('duplicate price history key')
        price_index[ident] = row
        o, high, low, c, volume = [float(row[k]) for k in ('open', 'high', 'low', 'close', 'volume_btc')]
        if not all(math.isfinite(v) for v in (o, high, low, c, volume)) or not 0 < low <= min(o,c) <= max(o,c) <= high or volume < 0:
            raise ValueError('invalid historical OHLCV')
        step = STEPS[row['timeframe']]
        opened = parse_time(row['start_utc']).timestamp()
        ended = parse_time(row['end_utc']).timestamp()
        if opened % step or ended != opened + step or ended > clock.timestamp():
            raise ValueError('unclosed or misaligned price history')
    checks = {}
    for exchange, currency in (('binance', 'USDT'), ('coinbase', 'USD')):
        market = data['markets'][exchange]
        if market['quote_currency'] != currency or market['cvd'] != legacy['markets'][exchange]:
            raise ValueError('quote unit or legacy CVD compatibility mismatch')
        checks[exchange] = {}
        for frame, step in STEPS.items():
            item = market[frame]
            start, end = parse_time(item['start_utc']).timestamp(), parse_time(item['end_utc']).timestamp()
            if start % step or end % step or end - start != 7 * 86400:
                raise ValueError('not exactly seven days of aligned closed candles')
            expected = set(range(int(start), int(end), step))
            observed = [int(parse_time(c['start_utc']).timestamp()) for c in item['candles']]
            missing = sorted(expected - set(observed))
            if len(observed) != len(set(observed)) or observed != sorted(observed) or not set(observed) <= expected:
                raise ValueError('duplicate/out-of-range/unsorted JSON candles')
            if item['expected'] != len(expected) or item['intervals'] != len(observed) or [int(parse_time(v).timestamp()) for v in item['missing_intervals']] != missing:
                raise ValueError('misreported seven-day coverage')
            if item['coverage'] != ('incomplete' if missing else 'complete'):
                raise ValueError('false candle completeness')
            for candle in item['candles']:
                t = parse_time(candle['start_utc']).timestamp()
                stored = price_index[(exchange, frame, t)]
                if stored['quote_currency'] != currency or candle['end_utc'] != stored['end_utc']:
                    raise ValueError('CSV and JSON candle metadata differ')
                for field in ('open', 'high', 'low', 'close', 'volume_btc'):
                    close(candle[field], stored[field], field)
            indicator = item['indicators']
            if indicator:
                lower = parse_time(indicator['calculation_start_utc']).timestamp()
                rows = [price_index[(exchange, frame, t)] for t in range(int(lower), int(end), step)]
                values = [float(r['close']) for r in rows]
                if len(rows) < 60 or indicator['input_candles'] != len(rows) or indicator['as_of_utc'] != item['end_utc'] or indicator['source'] != item['source'] or indicator['price_unit'] != currency:
                    raise ValueError('indicator calculation window invalid')
                close(indicator['ema20'], reference_ema(values, 20), 'EMA20')
                close(indicator['ema60'], reference_ema(values, 60), 'EMA60')
                close(indicator['rsi14'], reference_rsi(values), 'RSI14')
                close(indicator['support_reference'], min(float(r['low']) for r in rows[-20:]), 'support')
                close(indicator['resistance_reference'], max(float(r['high']) for r in rows[-20:]), 'resistance')
                prev = float(rows[-2]['volume_btc'])
                mean = sum(float(r['volume_btc']) for r in rows[-21:-1]) / 20
                volume = float(rows[-1]['volume_btc'])
                for name, denominator in (('volume_change_pct', prev), ('volume_vs_prior20_pct', mean)):
                    if denominator:
                        close(indicator[name], (volume / denominator - 1) * 100, name)
                    elif indicator[name] is not None:
                        raise ValueError('zero volume denominator must be null')
            checks[exchange][frame] = {'coverage': item['coverage'], 'intervals': len(observed), 'expected': len(expected), 'indicators_verified': bool(indicator)}
    cvd = load_rows(directory / 'cvd_history.csv')
    cvd_index = {}
    for row in cvd:
        t = int(parse_time(row['start_utc']).timestamp())
        key = row['exchange'], t
        if key in cvd_index or t % 300 or parse_time(row['end_utc']).timestamp() != t + 300:
            raise ValueError('duplicate/misaligned CVD history')
        cvd_index[key] = row
        if row['coverage'] == 'complete':
            buy, sell = Decimal(row['buy_usd']), Decimal(row['sell_usd'])
            tolerance = Decimal('.011') if row.get('rounding_policy') == 'legacy_observation_retained_max_0.01_quote_rounding' else Decimal('.001')
            if not buy.is_finite() or not sell.is_finite() or min(buy, sell) < 0 or abs(Decimal(row['cvd_usd']) - (buy-sell)) > tolerance or not row['source']:
                raise ValueError('CVD arithmetic/source invalid')
            if sell:
                close(row['taker_ratio'], buy/sell, 'CVD ratio', tolerance=.00004)
            elif row['taker_ratio'] not in ('', 'None'):
                raise ValueError('zero sell CVD ratio must be unavailable')
        elif any(row[k] not in ('', 'None') for k in ('buy_usd', 'sell_usd', 'cvd_usd', 'taker_ratio')):
            raise ValueError('unavailable CVD must not contain invented amounts')
    for exchange in checks:
        coverage = data['markets'][exchange]['historical_cvd']
        start = int(parse_time(coverage['start_utc']).timestamp())
        end = int(parse_time(coverage['end_utc']).timestamp())
        available = [t for t in range(start, end, 300) if cvd_index.get((exchange,t), {}).get('coverage') == 'complete']
        missing = sorted(set(range(start,end,300)) - set(available))
        if end-start != 7*86400 or coverage['available'] != len(available) or coverage['unavailable'] != len(missing) or [int(parse_time(v).timestamp()) for v in coverage['unavailable_intervals']] != missing or coverage['direction_inferred_from_price']:
            raise ValueError('historical CVD coverage misreported')
        if missing and coverage['net_cvd_quote'] is not None:
            raise ValueError('incomplete historical CVD cannot report a full-period net sum')
        if not missing:
            close(coverage['net_cvd_quote'], sum(Decimal(cvd_index[(exchange,t)]['cvd_usd']) for t in available), 'seven-day CVD', tolerance=.011)
    checked = refresh_lfm3_quality(data, clock)
    if require_usable and not checked['monitoring_eligible']:
        raise ValueError('LFM3 realtime price/CVD data stale or incomplete: ' + json.dumps(checked['errors']))
    return {'status': 'PASS', 'checks': checks, 'price_rows': len(prices), 'cvd_rows': len(cvd),
            'quality_status': checked['quality_status'], 'historical_cvd_quality': checked['historical_cvd_quality'],
            'data_age_seconds': checked['data_age_seconds'], 'market_data_age_seconds': checked['market_data_age_seconds']}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir', type=Path, default=Path(__file__).parent / 'data')
    parser.add_argument('--allow-partial', action='store_true')
    args = parser.parse_args()
    print(json.dumps(validate(args.data_dir, not args.allow_partial), indent=2))
