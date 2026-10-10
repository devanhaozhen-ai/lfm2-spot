"""Validate timestamps, history coverage, units, types, and CVD independently."""
import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from quality import refresh_quality, parse_time


def require(condition, message):
    if not condition:
        raise ValueError(message)


def timestamp(value):
    return parse_time(value).timestamp()


def metrics(row, label, json_numbers=False):
    if json_numbers:
        for key in ('buy_usd', 'sell_usd', 'cvd_usd'):
            require(type(row[key]) in (int, float), label + ': incorrect numeric type')
        require(row['taker_ratio'] is None or type(row['taker_ratio']) in (int, float), label + ': incorrect ratio type')
    buy, sell, cvd = (float(row[key]) for key in ('buy_usd', 'sell_usd', 'cvd_usd'))
    require(all(math.isfinite(v) for v in (buy, sell, cvd)), label + ': non-finite metric')
    require(buy >= 0 and sell >= 0, label + ': negative volume')
    # Existing rows may independently round buy, sell, and raw CVD by one cent.
    require(abs(cvd - (buy - sell)) <= .021, label + ': CVD mismatch')
    ratio = row['taker_ratio']
    if sell:
        require(ratio is not None and math.isfinite(float(ratio)) and math.isclose(float(ratio), buy / sell, abs_tol=.00003, rel_tol=.00003), label + ': taker ratio mismatch')
    else:
        require(ratio in (None, ''), label + ': ratio must be null with zero sell volume')


def validate(root, require_complete=False):
    data = json.loads((root / 'latest.json').read_text())
    with (root / 'history.csv').open(newline='') as handle:
        history = list(csv.DictReader(handle))
    require(data['schema_version'] == '1.0', 'unexpected JSON schema')
    require(set(data['markets']) == {'binance', 'coinbase'}, 'missing exchange')
    end = timestamp(data['latest_closed_5m_end_utc'])
    require(end % 300 == 0, 'window end is not aligned to five minutes')
    keys = [(r['exchange'], timestamp(r['start_utc'])) for r in history]
    require(len(set(keys)) == len(keys), 'duplicate history interval')
    for row in history:
        require(row['exchange'] in ('binance', 'coinbase'), 'unknown history exchange')
        started, ended = timestamp(row['start_utc']), timestamp(row['end_utc'])
        require(started % 300 == 0 and ended - started == 300, 'invalid history interval boundary')
        require(row['coverage'] in ('complete', 'incomplete', 'unavailable'), 'unknown coverage status')
        require(int(row['trades']) >= 0, 'negative history trade count')
        if row['coverage'] == 'complete':
            metrics(row, row['exchange'] + ' history')
    latest = {r['exchange']: r for r in data['last_intervals']}
    require(len(data['last_intervals']) == 2 and set(latest) == {'binance', 'coinbase'}, 'missing latest interval')
    for exchange, row in latest.items():
        require(timestamp(row['end_utc']) == end and timestamp(row['start_utc']) == end - 300, exchange + ': wrong latest interval')
        require(type(row['trades']) is int and row['trades'] >= 0, exchange + ': invalid trade count type')
        if row['coverage'] == 'complete':
            metrics(row, exchange + ' 5m', True)
        else:
            require(all(row[key] is None for key in ('buy_usd','sell_usd','cvd_usd','taker_ratio')), exchange + ': partial latest interval has directional metrics')
        if require_complete:
            require(row['coverage'] == 'complete', exchange + ': latest interval incomplete')
        for frame, count in (('15m', 3), ('60m', 12)):
            window = data['markets'][exchange][frame]
            expected = set(range(int(end) - count * 300, int(end), 300))
            rows = [r for r in history if r['exchange'] == exchange and r['coverage'] == 'complete' and end - count * 300 <= timestamp(r['start_utc']) < end]
            actual = {timestamp(r['start_utc']) for r in rows}
            missing = sorted(expected - actual)
            complete = len(rows) == count and actual == expected
            require(window['expected'] == count and window['intervals'] == len(actual & expected), exchange + ' ' + frame + ': interval count mismatch')
            require((window['coverage'] == 'complete') == complete, exchange + ' ' + frame + ': incorrect coverage')
            require([timestamp(t) for t in window['missing_intervals']] == missing, exchange + ' ' + frame + ': missing interval list mismatch')
            if complete:
                metrics(window, exchange + ' ' + frame, True)
                for key in ('buy_usd', 'sell_usd', 'cvd_usd'):
                    require(abs(float(window[key]) - sum(float(r[key]) for r in rows)) <= .02 + count * .01, exchange + ' ' + frame + ': ' + key + ' differs from history')
            else:
                require(all(window[key] is None for key in ('buy_usd', 'sell_usd', 'cvd_usd', 'taker_ratio')), exchange + ' ' + frame + ': incomplete window has metrics')
            if require_complete:
                require(complete, exchange + ' ' + frame + ': incomplete window')
            print(exchange + ' ' + frame + ': ' + window['coverage'] + ' (' + str(len(rows)) + '/' + str(count) + ' intervals)')
    units = data['market_units']
    require(units['binance']['quote_currency'] == 'USDT' and units['coinbase']['quote_currency'] == 'USD', 'incorrect quote units')
    require(data['cross_exchange_notional_aggregation'] == 'disabled', 'unconverted USDT/USD aggregation must be disabled')
    require(all(units[e]['conversion_to_usd_applied'] is False for e in units), 'unexpected currency conversion')
    for field in ('data_age_seconds', 'quality_status', 'missing_intervals', 'quality_evaluated_utc', 'stale_after_seconds'):
        require(field in data, 'missing quality field: ' + field)
    stored = refresh_quality(data, parse_time(data['quality_evaluated_utc']))
    require(stored['quality_status'] == data['quality_status'], 'stored quality status is inconsistent with its evaluation time')
    checked = refresh_quality(data)
    if require_complete:
        require(checked['monitoring_eligible'], 'data is stale or incomplete at read time')
    history_gaps = {}
    for exchange in ('binance', 'coinbase'):
        starts = {int(timestamp(r['start_utc'])) for r in history if r['exchange'] == exchange and r['coverage'] == 'complete'}
        expected = set(range(min(starts), int(end), 300)) if starts else set()
        history_gaps[exchange] = len(expected - starts)
    print(json.dumps({'quality_status': checked['quality_status'], 'data_age_seconds': checked['data_age_seconds'], 'market_data_age_seconds': checked['market_data_age_seconds'], 'history_rows': len(history), 'historical_missing_intervals': history_gaps}, allow_nan=False))
    print('CVD, taker ratios, timestamps, units, quality, and history consistency: PASS')
    return checked


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--require-complete', action='store_true')
    args = parser.parse_args()
    validate(Path(__file__).parent / 'data', args.require_complete)
