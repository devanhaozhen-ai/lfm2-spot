"""Check published metrics against their timestamped source intervals."""
import argparse
import csv
import json
import math
from datetime import datetime, timezone
from pathlib import Path


def require(condition, message):
    if not condition:
        raise ValueError(message)


def timestamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def metrics(row, label):
    buy, sell, cvd = (float(row[key]) for key in ('buy_usd', 'sell_usd', 'cvd_usd'))
    require(all(math.isfinite(v) for v in (buy, sell, cvd)), label + ': non-finite metric')
    require(buy >= 0 and sell >= 0, label + ': negative volume')
    require(abs(cvd - (buy - sell)) <= 0.021, label + ': CVD mismatch')
    ratio = row['taker_ratio']
    if sell:
        require(ratio is not None and math.isclose(float(ratio), buy / sell, abs_tol=0.00003, rel_tol=0.00003), label + ': taker ratio mismatch')
    else:
        require(ratio in (None, ''), label + ': ratio must be null with zero sell volume')


def validate(root, require_complete=False):
    data = json.loads((root / 'latest.json').read_text())
    with (root / 'history.csv').open(newline='') as handle:
        history = list(csv.DictReader(handle))
    require(data['schema_version'] == '1.0', 'unexpected JSON schema')
    require(set(data['markets']) == {'binance', 'coinbase'}, 'missing exchange')
    end = int(timestamp(data['latest_closed_5m_end_utc']))
    require(end % 300 == 0, 'window end is not aligned to five minutes')
    age = datetime.now(timezone.utc).timestamp() - timestamp(data['generated_utc'])
    require(-60 <= age <= 900, 'JSON is stale or has a future timestamp')
    keys = [(r['exchange'], r['start_utc']) for r in history]
    require(len(set(keys)) == len(keys), 'duplicate history interval')
    latest = {r['exchange']: r for r in data['last_intervals']}
    require(len(data['last_intervals']) == 2 and set(latest) == {'binance', 'coinbase'}, 'missing latest interval')
    for exchange, row in latest.items():
        require(timestamp(row['end_utc']) == end and timestamp(row['start_utc']) == end - 300, exchange + ': wrong latest interval')
        if row['coverage'] == 'complete':
            metrics(row, exchange + ' 5m')
        if require_complete:
            require(row['coverage'] == 'complete', exchange + ': latest interval incomplete')
        for frame, count in (('15m', 3), ('60m', 12)):
            window = data['markets'][exchange][frame]
            expected = set(range(end - count * 300, end, 300))
            rows = [r for r in history if r['exchange'] == exchange and r['coverage'] == 'complete' and int(timestamp(r['start_utc'])) in expected]
            complete = len(rows) == count and {int(timestamp(r['start_utc'])) for r in rows} == expected and all(timestamp(r['end_utc']) == timestamp(r['start_utc']) + 300 for r in rows)
            require(window['expected'] == count and window['intervals'] == len(rows), exchange + ' ' + frame + ': interval count mismatch')
            require((window['coverage'] == 'complete') == complete, exchange + ' ' + frame + ': incorrect coverage')
            if complete:
                metrics(window, exchange + ' ' + frame)
                for key in ('buy_usd', 'sell_usd', 'cvd_usd'):
                    require(abs(float(window[key]) - sum(float(r[key]) for r in rows)) <= 0.02 + count * 0.01, exchange + ' ' + frame + ': ' + key + ' differs from history')
            else:
                require(all(window[key] is None for key in ('buy_usd', 'sell_usd', 'cvd_usd', 'taker_ratio')), exchange + ' ' + frame + ': incomplete window has metrics')
            if require_complete:
                require(complete, exchange + ' ' + frame + ': incomplete window')
            print(exchange + ' ' + frame + ': ' + window['coverage'] + ' (' + str(len(rows)) + '/' + str(count) + ' intervals)')
    if require_complete:
        require(not data['errors'], 'collector reported API errors')
    print('CVD, taker ratios, timestamps, and history consistency: PASS')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--require-complete', action='store_true')
    args = parser.parse_args()
    validate(Path(__file__).parent / 'data', args.require_complete)
