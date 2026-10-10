"""Collect public BTC spot taker flow; retain every previously valid interval."""
import csv
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

ROOT = Path(__file__).parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
HEAD = {'User-Agent': 'LFM2-Spot-Monitor/2.0', 'Accept': 'application/json'}
FETCH_DEADLINE = None
FETCH_STATS = {}
FIVE_MINUTES = 300000
FIELDS = ['exchange', 'start_utc', 'end_utc', 'buy_usd', 'sell_usd', 'cvd_usd', 'taker_ratio', 'trades', 'coverage', 'reason']


def utc(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat().replace('+00:00', 'Z')


def timestamp_ms(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp must include a UTC offset')
    return int(parsed.timestamp() * 1000)


def log(event, **values):
    print(json.dumps({'utc': utc(int(time.time() * 1000)), 'event': event, **values}, allow_nan=False), file=sys.stderr, flush=True)


def remaining_seconds():
    if FETCH_DEADLINE is None:
        return 20
    remaining = FETCH_DEADLINE - time.monotonic()
    if remaining <= 0:
        raise TimeoutError('collector network deadline reached')
    return min(20, remaining)


def get(url, params=None):
    if params:
        url += '?' + urllib.parse.urlencode(params)
    host = urllib.parse.urlsplit(url).hostname
    stats = FETCH_STATS.setdefault(host, {'requests': 0, 'retries': 0})
    for attempt in range(4):
        stats['requests'] += 1
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=HEAD), timeout=remaining_seconds()) as response:
                return json.load(response), {k.lower(): v for k, v in response.headers.items()}
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as error:
            status = error.code if isinstance(error, urllib.error.HTTPError) else None
            retryable = status is None or status in (408, 418, 429, 500, 502, 503, 504)
            log('api_error', host=host, attempt=attempt + 1, status=status, error_type=type(error).__name__, will_retry=retryable and attempt < 3)
            if not retryable or attempt == 3:
                raise
            delay = 2 ** attempt
            if isinstance(error, urllib.error.HTTPError):
                try:
                    delay = max(delay, float((error.headers or {}).get('Retry-After', 0)))
                except (TypeError, ValueError):
                    pass
            delay = min(delay, 30)
            if FETCH_DEADLINE is not None and time.monotonic() + delay >= FETCH_DEADLINE:
                raise TimeoutError('retry would exceed collector deadline') from error
            stats['retries'] += 1
            time.sleep(delay)


def atomic_write(path, writer):
    """A failed write leaves the previous published file intact."""
    path.parent.mkdir(exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix=path.name + '.', suffix='.tmp', newline='', encoding='utf-8', delete=False) as handle:
            temporary = Path(handle.name)
            writer(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def save(rows):
    path = DATA / 'history.csv'
    old = {}
    if path.exists():
        with path.open(newline='') as handle:
            for row in csv.DictReader(handle):
                old[(row['exchange'], timestamp_ms(row['start_utc']))] = row
    for row in rows:
        key = (row['exchange'], timestamp_ms(row['start_utc']))
        # Keep successful source observations immutable, including on failed refetches.
        if key in old and old[key]['coverage'] == 'complete':
            continue
        old[key] = row
    values = sorted(old.values(), key=lambda row: (timestamp_ms(row['start_utc']), row['exchange']))
    def write(handle):
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(values)
    atomic_write(path, write)


def entry(exchange, start, end, buy, sell, trades, coverage, reason=''):
    if coverage == 'complete':
        raw_buy, raw_sell = Decimal(str(buy)), Decimal(str(sell))
        buy = raw_buy.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        sell = raw_sell.quantize(Decimal('.01'), rounding=ROUND_HALF_UP)
        if not buy.is_finite() or not sell.is_finite() or buy < 0 or sell < 0:
            raise ValueError('invalid taker notional')
        cvd = float(buy - sell)
        ratio = round(float(raw_buy / raw_sell), 5) if sell and raw_sell else None
        buy, sell = float(buy), float(sell)
    else:
        buy = sell = cvd = ratio = None
    return dict(exchange=exchange, start_utc=utc(start), end_utc=utc(end), buy_usd=buy, sell_usd=sell, cvd_usd=cvd, taker_ratio=ratio, trades=trades, coverage=coverage, reason=reason)


def binance(start, end):
    candles, _ = get('https://data-api.binance.vision/api/v3/klines', {'symbol': 'BTCUSDT', 'interval': '1m', 'startTime': start, 'endTime': end - 1, 'limit': 1000})
    if not isinstance(candles, list):
        raise ValueError('Binance response is not a candle list')
    by_start = {}
    for candle in candles:
        opened = int(candle[0])
        if opened in by_start or not start <= opened < end:
            raise ValueError('duplicate or out-of-range Binance candle')
        by_start[opened] = candle
    rows = []
    for slot in range(start, end, FIVE_MINUTES):
        expected = list(range(slot, slot + FIVE_MINUTES, 60000))
        if any(t not in by_start or int(by_start[t][6]) != t + 59999 or int(by_start[t][6]) >= end for t in expected):
            rows.append(entry('binance', slot, slot + FIVE_MINUTES, 0, 0, 0, 'incomplete', 'missing or unclosed 1-minute candles'))
            continue
        selected = [by_start[t] for t in expected]
        buy = total = Decimal(0)
        for candle in selected:
            quote, taker = Decimal(candle[7]), Decimal(candle[10])
            if not quote.is_finite() or not taker.is_finite() or not 0 <= taker <= quote or int(candle[8]) < 0:
                raise ValueError('invalid Binance quote volume or trade count')
            buy += taker
            total += quote
        rows.append(entry('binance', slot, slot + FIVE_MINUTES, buy, total - buy, sum(int(c[8]) for c in selected), 'complete', '1m closed candle taker quote volumes'))
    FETCH_STATS['binance'] = {'candles': len(candles), 'expected_candles': (end - start) // 60000, 'quote_currency': 'USDT'}
    return rows


def coinbase(start, end):
    buckets = {slot: [Decimal(0), Decimal(0), 0] for slot in range(start, end, FIVE_MINUTES)}
    seen, cursors = {}, set()
    cursor = None
    oldest = newest = None
    duplicates = pages = 0
    stopped = 'pagination limit reached'
    for page in range(80):
        params = {'limit': 1000}
        if cursor is not None:
            params['after'] = cursor
        trades, headers = get('https://api.exchange.coinbase.com/products/BTC-USD/trades', params)
        if not isinstance(trades, list):
            raise ValueError('Coinbase response is not a trade list')
        if not trades:
            stopped = 'empty page before full window coverage'
            break
        pages += 1
        ids = [int(t['trade_id']) for t in trades]
        if ids != sorted(ids, reverse=True):
            raise ValueError('Coinbase page trade IDs are not descending')
        for trade in trades:
            ident = int(trade['trade_id'])
            if ident in seen:
                if seen[ident] != trade:
                    raise ValueError('conflicting Coinbase duplicate trade')
                duplicates += 1
                continue
            seen[ident] = trade
            moment = timestamp_ms(trade['time'])
            oldest = moment if oldest is None else min(oldest, moment)
            newest = moment if newest is None else max(newest, moment)
            if not start <= moment < end:
                continue
            price, size = Decimal(trade['price']), Decimal(trade['size'])
            if not price.is_finite() or not size.is_finite() or price <= 0 or size < 0 or trade['side'] not in ('sell', 'buy'):
                raise ValueError('invalid Coinbase trade side, price, or size')
            bucket = buckets[(moment // FIVE_MINUTES) * FIVE_MINUTES]
            # Coinbase REST side is MAKER side: maker sell => taker buy.
            bucket[0 if trade['side'] == 'sell' else 1] += price * size
            bucket[2] += 1
        if oldest is not None and oldest < start:
            stopped = 'reached before requested start'
            break
        next_cursor = next((v for k, v in headers.items() if k.lower() == 'cb-after'), None)
        if not next_cursor or next_cursor in cursors:
            stopped = 'missing or repeated pagination cursor'
            break
        if str(next_cursor) != str(ids[-1]):
            stopped = 'cursor does not match page oldest trade ID'
            break
        cursors.add(next_cursor)
        cursor = next_cursor
        time.sleep(.22)
    ordered_ids = sorted(seen, reverse=True)
    gaps = sum(a - b > 1 for a, b in zip(ordered_ids, ordered_ids[1:]))
    snapshot_covers_end = newest is not None and newest >= end
    FETCH_STATS['coinbase'] = {'pages': pages, 'unique_trades': len(seen), 'duplicate_trades': duplicates, 'trade_id_gaps': gaps, 'newest_trade_utc': utc(newest) if newest is not None else None, 'oldest_trade_utc': utc(oldest) if oldest is not None else None, 'snapshot_covers_end': snapshot_covers_end, 'stop_reason': stopped, 'quote_currency': 'USD'}
    rows = []
    for slot, (buy, sell, trades) in buckets.items():
        complete = oldest is not None and oldest < slot and snapshot_covers_end and not gaps
        reason = '' if complete else ('trade ID continuity unverified' if gaps else 'snapshot does not cover interval end' if not snapshot_covers_end else stopped)
        rows.append(entry('coinbase', slot, slot + FIVE_MINUTES, buy, sell, trades, 'complete' if complete else 'incomplete', reason))
    return rows


def windows_for(history, end):
    windows = {}
    for exchange in ('binance', 'coinbase'):
        windows[exchange] = {}
        for minutes in (15, 60):
            lower = end - minutes * 60000
            expected = set(range(lower, end, FIVE_MINUTES))
            selected = [row for row in history if row['exchange'] == exchange and row['coverage'] == 'complete' and lower <= timestamp_ms(row['start_utc']) < end]
            valid = {timestamp_ms(row['start_utc']) for row in selected if timestamp_ms(row['end_utc']) == timestamp_ms(row['start_utc']) + FIVE_MINUTES}
            missing = sorted(expected - valid)
            complete = not missing and valid == expected and len(selected) == minutes // 5 and all(timestamp_ms(row['end_utc']) == timestamp_ms(row['start_utc']) + FIVE_MINUTES for row in selected)
            buy = sum((Decimal(str(r['buy_usd'])) for r in selected), Decimal(0)) if complete else None
            sell = sum((Decimal(str(r['sell_usd'])) for r in selected), Decimal(0)) if complete else None
            windows[exchange][str(minutes) + 'm'] = {'coverage': 'complete' if complete else 'incomplete', 'intervals': len(valid & expected), 'expected': minutes // 5, 'buy_usd': float(buy) if complete else None, 'sell_usd': float(sell) if complete else None, 'cvd_usd': float(buy - sell) if complete else None, 'taker_ratio': round(float(buy / sell), 5) if complete and sell else None, 'missing_intervals': [utc(t) for t in missing]}
    return windows


def backfill_start(end):
    starts = {exchange: set() for exchange in ('binance', 'coinbase')}
    first = {}
    if (DATA / 'history.csv').exists():
        with (DATA / 'history.csv').open(newline='') as handle:
            for row in csv.DictReader(handle):
                exchange = row['exchange']
                moment = timestamp_ms(row['start_utc'])
                first[exchange] = min(first.get(exchange, moment), moment)
                if row['coverage'] == 'complete':
                    starts[exchange].add(moment)
    lower = end - 6 * 3600000
    requested = end - 3600000
    # Look for holes as well as a missing tail. A later good row must not hide a gap.
    for exchange in starts:
        known_start = max(lower, first.get(exchange, requested))
        for moment in range(known_start, end, FIVE_MINUTES):
            if moment not in starts[exchange]:
                requested = min(requested, moment)
                break
    return max(lower, requested)


def history_quality(history, end):
    result = {}
    for exchange in ('binance', 'coinbase'):
        rows = [row for row in history if row['exchange'] == exchange]
        starts = {timestamp_ms(row['start_utc']) for row in rows if row['coverage'] == 'complete'}
        first = min((timestamp_ms(row['start_utc']) for row in rows), default=end)
        missing = [slot for slot in range(first, end, FIVE_MINUTES) if slot not in starts]
        result[exchange] = {'coverage': 'incomplete' if missing else 'complete',
                            'missing_count': len(missing),
                            'missing_intervals': [utc(slot) for slot in missing[:100]],
                            'missing_list_truncated': len(missing) > 100,
                            'outside_backfill_horizon': sum(slot < end - 6 * 3600000 for slot in missing)}
    return result


def main():
    global FETCH_DEADLINE
    started = int(time.time() * 1000)
    end = (started // FIVE_MINUTES) * FIVE_MINUTES
    start = backfill_start(end)
    FETCH_DEADLINE = time.monotonic() + 420
    FETCH_STATS.clear()
    latest_rows, all_rows, errors = [], [], {}
    for exchange, fetch in (('binance', binance), ('coinbase', coinbase)):
        try:
            fetched = fetch(start, end)
            if len(fetched) != (end - start) // FIVE_MINUTES:
                raise ValueError('missing five-minute interval results')
        except Exception as error:
            log('exchange_failed', exchange=exchange, error_type=type(error).__name__, reason=str(error))
            fetched = [entry(exchange, t, t + FIVE_MINUTES, 0, 0, 0, 'unavailable', str(error)) for t in range(start, end, FIVE_MINUTES)]
        failures = [row['reason'] for row in fetched if row['coverage'] != 'complete']
        if failures:
            errors[exchange] = '; '.join(sorted(set(failures)))
        all_rows.extend(fetched)
        latest_rows.append(fetched[-1])
    save(all_rows)
    with (DATA / 'history.csv').open(newline='') as handle:
        history = list(csv.DictReader(handle))
    generated = int(time.time() * 1000)
    out = {'schema_version': '1.0', 'generated_utc': utc(generated), 'latest_closed_5m_end_utc': utc(end), 'markets': windows_for(history, end), 'last_intervals': latest_rows, 'warnings': ['Coinbase side is maker side and must be inverted for taker direction.', 'Incomplete or stale data must not be used for directional signals.', 'Binance legacy *_usd fields are USDT amounts, not converted USD. Do not add Binance and Coinbase notionals without an exchange rate.'], 'errors': errors, 'market_units': {'binance': {'symbol': 'BTCUSDT', 'quote_currency': 'USDT', 'legacy_usd_fields_are_approximate': True, 'conversion_to_usd_applied': False}, 'coinbase': {'symbol': 'BTC-USD', 'quote_currency': 'USD', 'legacy_usd_fields_are_approximate': False, 'conversion_to_usd_applied': False}}, 'cross_exchange_notional_aggregation': 'disabled', 'collection_started_utc': utc(started), 'backfill_start_utc': utc(start), 'source_audit': FETCH_STATS}
    from quality import refresh_quality
    out['history_quality'] = history_quality(history, end)
    out = refresh_quality(out, datetime.fromtimestamp(generated / 1000, timezone.utc))
    atomic_write(DATA / 'latest.json', lambda handle: handle.write(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False) + '\n'))
    log('collection_complete', end=utc(end), quality_status=out['quality_status'], missing_intervals=out['missing_intervals'], source_audit=FETCH_STATS, errors=errors)


if __name__ == '__main__':
    main()
