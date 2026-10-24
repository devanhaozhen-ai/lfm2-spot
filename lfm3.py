"""Public, closed spot candles and measured taker flow. No inferred trade sides."""
import copy
import csv
import json
import math
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import collector
from quality import parse_time, refresh_quality

DAY = 86400000
FRAMES = {'5m': 300000, '1h': 3600000}
PRICE_FIELDS = ['exchange', 'symbol', 'quote_currency', 'timeframe', 'start_utc', 'end_utc',
                'open', 'high', 'low', 'close', 'volume_btc', 'quote_volume',
                'taker_buy_quote', 'trades', 'coverage', 'source']
CVD_FIELDS = collector.FIELDS + ['symbol', 'quote_currency', 'source', 'rounding_policy']
SOURCES = {'binance': 'https://data-api.binance.vision/api/v3/klines',
           'coinbase': 'https://api.exchange.coinbase.com/products/BTC-USD/candles'}
UNITS = {'binance': ('BTCUSDT', 'USDT'), 'coinbase': ('BTC-USD', 'USD')}


def read_csv(path):
    if not path.exists():
        return []
    with path.open(newline='') as handle:
        return list(csv.DictReader(handle))


def price_key(row):
    return row['exchange'], row['timeframe'], collector.timestamp_ms(row['start_utc'])


def cvd_key(row):
    return row['exchange'], collector.timestamp_ms(row['start_utc'])


def persist(path, incoming, fields, key):
    """Immutable complete observations; unavailable records may become measured."""
    previous = read_csv(path)
    index = {key(row): row for row in previous}
    if len(index) != len(previous):
        raise ValueError('duplicate persisted records: ' + str(path))
    for row in incoming:
        ident = key(row)
        if ident not in index or index[ident]['coverage'] != 'complete':
            index[ident] = row
    rows = sorted(index.values(), key=key)
    def write(handle):
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    collector.atomic_write(path, write)
    return read_csv(path)


def valid_price(row):
    values = [float(row[k]) for k in ('open', 'high', 'low', 'close', 'volume_btc')]
    if not all(math.isfinite(v) for v in values):
        raise ValueError('nonfinite candle')
    o, h, low, c, v = values
    if not 0 < low <= min(o, c) <= max(o, c) <= h or v < 0:
        raise ValueError('invalid candle prices or volume')
    for field in ('quote_volume', 'taker_buy_quote'):
        if row[field] not in (None, '') and (not math.isfinite(float(row[field])) or float(row[field]) < 0):
            raise ValueError('invalid quote volume')
    if row['taker_buy_quote'] not in (None, '') and Decimal(str(row['taker_buy_quote'])) > Decimal(str(row['quote_volume'])):
        raise ValueError('taker buy exceeds total quote volume')
    if row['trades'] not in (None, '') and int(row['trades']) < 0:
        raise ValueError('negative trade count')
    step = FRAMES[row['timeframe']]
    start = collector.timestamp_ms(row['start_utc'])
    if start % step or collector.timestamp_ms(row['end_utc']) != start + step:
        raise ValueError('candle timestamp alignment')


def fetch_prices(exchange, frame, start, end):
    step = FRAMES[frame]
    limit = 1000 if exchange == 'binance' else 299
    rows, seen = [], {}
    for lower in range(start, end, limit * step):
        upper = min(end, lower + limit * step)
        params = ({'symbol': 'BTCUSDT', 'interval': frame, 'startTime': lower,
                   'endTime': upper - 1, 'limit': 1000} if exchange == 'binance' else
                  {'granularity': step // 1000, 'start': collector.utc(lower), 'end': collector.utc(upper)})
        raw, _ = collector.get(SOURCES[exchange], params)
        if not isinstance(raw, list):
            raise ValueError('candle endpoint did not return a list')
        for c in raw:
            opened = int(c[0]) if exchange == 'binance' else int(c[0]) * 1000
            # Coinbase may return the inclusive end candle or older records.
            if not lower <= opened < upper:
                continue
            if exchange == 'binance' and int(c[6]) != opened + step - 1:
                raise ValueError('Binance candle is not closed at its expected boundary')
            symbol, currency = UNITS[exchange]
            row = dict(exchange=exchange, symbol=symbol, quote_currency=currency,
                       timeframe=frame, start_utc=collector.utc(opened), end_utc=collector.utc(opened + step),
                       open=c[1] if exchange == 'binance' else c[3],
                       high=c[2], low=c[3] if exchange == 'binance' else c[1],
                       close=c[4], volume_btc=c[5],
                       quote_volume=c[7] if exchange == 'binance' else None,
                       taker_buy_quote=c[10] if exchange == 'binance' else None,
                       trades=int(c[8]) if exchange == 'binance' else None,
                       coverage='complete', source=SOURCES[exchange])
            valid_price(row)
            if opened in seen:
                if seen[opened] != row:
                    raise ValueError('conflicting duplicate candle')
                continue
            seen[opened] = row
            rows.append(row)
        time.sleep(.12)
    return sorted(rows, key=lambda r: collector.timestamp_ms(r['start_utc']))


def ema(values, period):
    if len(values) < period:
        return None
    result = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for value in values[period:]:
        result += alpha * (value - result)
    return result


def rsi(values, period=14):
    if len(values) <= period:
        return None
    changes = [b - a for a, b in zip(values, values[1:])]
    gain = sum(max(c, 0) for c in changes[:period]) / period
    loss = sum(max(-c, 0) for c in changes[:period]) / period
    for change in changes[period:]:
        gain = (gain * (period - 1) + max(change, 0)) / period
        loss = (loss * (period - 1) + max(-change, 0)) / period
    return 50.0 if not gain and not loss else 100.0 if not loss else 100 - 100 / (1 + gain / loss)


def indicators(rows):
    values = [float(r['close']) for r in rows]
    recent = rows[-20:]
    previous = float(rows[-2]['volume_btc']) if len(rows) > 1 else 0
    volume = float(rows[-1]['volume_btc'])
    prior20 = [float(r['volume_btc']) for r in rows[-21:-1]]
    mean20 = sum(prior20) / len(prior20) if prior20 else 0
    return {'ema20': ema(values, 20), 'ema60': ema(values, 60), 'rsi14': rsi(values),
            'ema_method': 'SMA seed, alpha=2/(period+1), all contiguous persisted closes',
            'rsi_method': 'Wilder smoothing, initial 14 close-to-close changes; flat series=50',
            'calculation_start_utc': rows[0]['start_utc'], 'as_of_utc': rows[-1]['end_utc'],
            'input_candles': len(rows), 'price_unit': rows[-1]['quote_currency'],
            'volume_unit': 'BTC', 'volume_change_pct': (volume / previous - 1) * 100 if previous else None,
            'volume_vs_prior20_pct': (volume / mean20 - 1) * 100 if mean20 else None,
            'volume_change_window_candles': 2, 'volume_change_start_utc': rows[-2]['start_utc'],
            'volume_mean_start_utc': rows[-21]['start_utc'], 'volume_mean_end_utc': rows[-2]['end_utc'],
            'ema_periods': [20, 60], 'rsi_period': 14,
            'volume_reference_candles': len(prior20), 'support_reference': min(float(r['low']) for r in recent),
            'resistance_reference': max(float(r['high']) for r in recent),
            'levels_window_candles': len(recent), 'levels_start_utc': recent[0]['start_utc'],
            'levels_end_utc': recent[-1]['end_utc'], 'levels_method': 'lowest low / highest high; descriptive reference only',
            'source': rows[-1]['source']}


def public_candle(row):
    result = dict(row)
    for field in ('open', 'high', 'low', 'close', 'volume_btc', 'quote_volume', 'taker_buy_quote'):
        result[field] = float(row[field]) if row[field] not in ('', None) else None
    result['trades'] = int(row['trades']) if row['trades'] not in ('', None) else None
    return result


def frame_data(history, exchange, frame, end):
    step = FRAMES[frame]
    start = end - 7 * DAY
    all_rows = sorted([r for r in history if r['exchange'] == exchange and r['timeframe'] == frame and r['coverage'] == 'complete'], key=lambda r: collector.timestamp_ms(r['start_utc']))
    index = {collector.timestamp_ms(r['start_utc']): r for r in all_rows}
    expected = list(range(start, end, step))
    missing = [t for t in expected if t not in index]
    # Indicators cannot cross a missing interval; use the contiguous tail.
    tail, cursor = [], end - step
    while cursor in index:
        tail.append(index[cursor]); cursor -= step
    tail.reverse()
    return {'source': SOURCES[exchange], 'interval_seconds': step // 1000,
            'start_utc': collector.utc(start), 'end_utc': collector.utc(end),
            'coverage': 'incomplete' if missing else 'complete', 'expected': len(expected),
            'intervals': len(expected) - len(missing), 'missing_intervals': [collector.utc(t) for t in missing],
            'candles': [public_candle(index[t]) for t in expected if t in index],
            'latest_candle': public_candle(index[end - step]) if end - step in index else None,
            'indicators': indicators(tail) if len(tail) >= 60 else None}


def build_cvd(prices, old_flow, end):
    rows = []
    measured = {cvd_key(r): r for r in old_flow if r['coverage'] == 'complete'}
    for p in prices:
        if p['exchange'] != 'binance' or p['timeframe'] != '5m':
            continue
        start = collector.timestamp_ms(p['start_utc'])
        total, buy = Decimal(str(p['quote_volume'])), Decimal(str(p['taker_buy_quote']))
        row = collector.entry('binance', start, start + FRAMES['5m'], buy, total - buy,
                              int(p['trades']), 'complete', '5m closed candle taker quote volumes')
        row.update(symbol='BTCUSDT', quote_currency='USDT', source=SOURCES['binance'] + '#fields-7-and-10', rounding_policy='rounded_buy_minus_rounded_sell')
        rows.append(row)
    for start in range(end - 7 * DAY, end, FRAMES['5m']):
        old = measured.get(('coinbase', start))
        row = dict(old) if old else collector.entry('coinbase', start, start + FRAMES['5m'], 0, 0, 0,
                'unavailable', 'Historical maker-side trades not fully retrieved; candles contain no taker direction')
        if not old:
            row['trades'] = None
        row.update(symbol='BTC-USD', quote_currency='USD', source='https://api.exchange.coinbase.com/products/BTC-USD/trades' if old else '')
        row['rounding_policy'] = 'legacy_observation_retained_max_0.01_quote_rounding' if old else ''
        rows.append(row)
    # Preserve measured LFM2 observations, including those older than seven days.
    for old in old_flow:
        if old['coverage'] != 'complete':
            continue
        row = dict(old)
        symbol, currency = UNITS[old['exchange']]
        row.update(symbol=symbol, quote_currency=currency,
                   source='https://api.exchange.coinbase.com/products/BTC-USD/trades' if old['exchange'] == 'coinbase' else SOURCES['binance'] + '#1m-fields-7-and-10',
                   rounding_policy='legacy_observation_retained_max_0.01_quote_rounding')
        rows.append(row)
    return rows


def cvd_coverage(history, exchange, end):
    start = end - 7 * DAY
    index = {collector.timestamp_ms(r['start_utc']): r for r in history if r['exchange'] == exchange}
    available = [t for t in range(start, end, FRAMES['5m']) if t in index and index[t]['coverage'] == 'complete']
    missing = [t for t in range(start, end, FRAMES['5m']) if t not in available]
    return {'start_utc': collector.utc(start), 'end_utc': collector.utc(end), 'timeframe': '5m',
            'expected': 2016, 'available': len(available), 'unavailable': len(missing),
            'coverage': 'complete' if not missing else 'partial' if available else 'unavailable',
            'unavailable_intervals': [collector.utc(t) for t in missing],
            'net_cvd_quote': float(sum((Decimal(str(index[t]['cvd_usd'])) for t in available), Decimal(0))) if not missing else None,
            'quote_currency': UNITS[exchange][1], 'direction_inferred_from_price': False,
            'source': SOURCES['binance'] + '#taker-buy-quote' if exchange == 'binance' else 'https://api.exchange.coinbase.com/products/BTC-USD/trades',
            'reason': '' if not missing else 'No complete, verifiable historical taker-side observations for these intervals'}


def refresh_lfm3_quality(data, clock=None):
    """Recalculate source ages, including hour candle expected closure, at read time."""
    result = copy.deepcopy(data)
    clock = clock or datetime.now(timezone.utc)
    age = (clock - parse_time(result['generated_utc'])).total_seconds()
    source_age = (clock - parse_time(result['latest_closed_candle_utc'])).total_seconds()
    result.update(data_age_seconds=round(age, 3), market_data_age_seconds=round(source_age, 3),
                  stale_after_seconds=900, quality_evaluated_utc=clock.isoformat().replace('+00:00', 'Z'))
    good = []
    for exchange, market in result['markets'].items():
        complete = not result['errors'].get(exchange)
        for frame, step in FRAMES.items():
            item = market[frame]
            expected = int(clock.timestamp() * 1000) // step * step
            candle = item['latest_candle']
            end = collector.timestamp_ms(candle['end_utc']) if candle else 0
            item['data_age_seconds'] = round((clock.timestamp() * 1000 - end) / 1000, 3) if candle else None
            item['lag_from_expected_close_seconds'] = (expected - end) / 1000 if candle else None
            item['fresh'] = bool(candle) and 0 <= expected - end <= 900000 and end <= clock.timestamp() * 1000
            complete = complete and item['coverage'] == 'complete' and item['intervals'] == item['expected'] and not item['missing_intervals'] and item['fresh'] and item['indicators'] is not None
        complete = complete and all(market['cvd'][f]['coverage'] == 'complete' and market['cvd'][f]['intervals'] == n for f, n in (('15m', 3), ('60m', 12)))
        market['quality_status'] = 'ok' if complete else 'incomplete'
        good.append(complete)
    stale = age > 900 or source_age > 900 or age < 0 or source_age < 0
    result['quality_status'] = 'stale' if stale else 'ok' if all(good) else 'incomplete'
    result['monitoring_eligible'] = result['quality_status'] == 'ok' and not result['errors']
    result['historical_cvd_quality'] = 'complete' if all(m['historical_cvd']['coverage'] == 'complete' for m in result['markets'].values()) else 'partial'
    return result


def collect(data_dir=None):
    data_dir = Path(data_dir or collector.DATA)
    baseline = json.loads((data_dir / 'latest.json').read_text())
    end = collector.timestamp_ms(baseline['latest_closed_5m_end_utc'])
    errors, incoming = {}, []
    collector.FETCH_DEADLINE = time.monotonic() + 240
    collector.FETCH_STATS.clear()
    for exchange in UNITS:
        for frame, step in FRAMES.items():
            closed = end // step * step
            try:
                incoming.extend(fetch_prices(exchange, frame, closed - 7 * DAY, closed))
            except Exception as exc:
                errors.setdefault(exchange, {})[frame] = str(exc)
                collector.log('lfm3_price_failure', exchange=exchange, frame=frame, reason=str(exc))
    prices = persist(data_dir / 'price_history.csv', incoming, PRICE_FIELDS, price_key)
    cvd = persist(data_dir / 'cvd_history.csv', build_cvd(prices, read_csv(data_dir / 'history.csv'), end), CVD_FIELDS, cvd_key)
    markets = {}
    for exchange, (symbol, currency) in UNITS.items():
        market = {'symbol': symbol, 'quote_currency': currency,
                  **{frame: frame_data(prices, exchange, frame, end // step * step) for frame, step in FRAMES.items()},
                  'cvd': copy.deepcopy(baseline['markets'][exchange]),
                  'cvd_as_of_utc': baseline['latest_closed_5m_end_utc'],
                  'cvd_source': 'Binance 1m taker quote volumes' if exchange == 'binance' else 'Coinbase paginated maker-side trades, inverted to taker side',
                  'cvd_window_metadata': {frame: {'start_utc': collector.utc(end - minutes * 60000),
                      'end_utc': collector.utc(end), 'window_minutes': minutes, 'quote_currency': currency,
                      'interval_semantics': '[start_utc, end_utc)',
                      'source': SOURCES['binance'] + '#1m-taker-buy-quote' if exchange == 'binance' else 'https://api.exchange.coinbase.com/products/BTC-USD/trades'} for frame, minutes in (('15m',15), ('60m',60))},
                  'historical_cvd': cvd_coverage(cvd, exchange, end)}
        if baseline['errors'].get(exchange):
            errors.setdefault(exchange, {})['live_cvd'] = baseline['errors'][exchange]
        for frame in FRAMES:
            if market[frame]['coverage'] != 'complete':
                errors.setdefault(exchange, {})[frame] = 'Missing closed candles: ' + str(len(market[frame]['missing_intervals']))
        markets[exchange] = market
    generated = collector.utc(int(time.time() * 1000))
    out = dict(schema_version='3.0', generated_utc=generated, latest_closed_candle_utc=collector.utc(end),
               collection_mode=baseline.get('collection_mode', 'manual_workflow_dispatch'), request_id=baseline['request_id'],
               collection_run_id=baseline['collection_run_id'], lfm2_generated_utc=baseline['generated_utc'],
               markets=markets, errors=errors,
               warnings=['USDT and USD are separate quote currencies; no conversion or cross-exchange addition.',
                         'Coinbase candles do not identify taker direction. Unretrieved historical CVD is unavailable, never inferred.',
                         'Static quality must be recalculated using current UTC; backfill is not proof of freshness.',
                         'Legacy historical observations remain unchanged; independently rounded amounts may differ by 0.01 quote unit, annotated per CVD row.',
                         '1h candle freshness uses lag from the most recent expected hourly closure, not raw hourly candle age.'],
               history_files={'price': 'data/price_history.csv', 'cvd': 'data/cvd_history.csv', 'lfm2': 'data/history.csv'},
               source_audit=copy.deepcopy(collector.FETCH_STATS), cross_exchange_notional_aggregation='disabled')
    out = refresh_lfm3_quality(out)
    # Legacy long-range continuity is independent from LFM3's verified live
    # candles and CVD windows. Expose it, never silently erase its warning.
    # LFM3 live eligibility is still gated by its own freshness, source errors,
    # price coverage, indicators, and measured 15m/60m CVD coverage.
    old_quality = refresh_quality(baseline)
    out['legacy_history_quality'] = {
        'quality_status_at_collection': old_quality['quality_status'],
        'monitoring_eligible_at_collection': old_quality['monitoring_eligible'],
        'history_quality': baseline.get('history_quality', {}),
    }
    if not old_quality['monitoring_eligible']:
        out['warnings'].append('Legacy LFM2 history is incomplete or stale; LFM3 live quality is evaluated independently.')
    collector.atomic_write(data_dir / 'lfm3_latest.json', lambda h: h.write(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False) + '\n'))
    collector.log('lfm3_complete', quality_status=out['quality_status'], errors=errors,
                  price_rows=len(prices), cvd_rows=len(cvd))
    return out


if __name__ == '__main__':
    collect()
