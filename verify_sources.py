"""Independently recompute closed spot intervals from exchange REST responses."""
import csv
import json
import math
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from quality import parse_time


def milliseconds(value):
    return int(parse_time(value).timestamp() * 1000)


def request(url, params, deadline):
    url += '?' + urllib.parse.urlencode(params)
    for attempt in range(3):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError('independent source verification deadline reached')
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'LFM2-Independent-Manual-Verification', 'Accept': 'application/json'}), timeout=min(20, remaining)) as response:
                return json.load(response), {k.lower(): v for k, v in response.headers.items()}
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(1 + attempt)


def compare(row, buy, sell, trades):
    if row['coverage'] != 'complete' or int(row['trades']) != trades:
        raise ValueError('source coverage or trade count differs from saved interval')
    for field, value in (('buy_usd', buy), ('sell_usd', sell), ('cvd_usd', buy - sell)):
        if abs(Decimal(str(row[field])) - value) > Decimal('.011'):
            raise ValueError('independent source ' + field + ' mismatch')
    if sell:
        if not math.isclose(float(row['taker_ratio']), float(buy / sell), rel_tol=.00003, abs_tol=.00003):
            raise ValueError('independent source taker ratio mismatch')
    elif row['taker_ratio'] not in (None, ''):
        raise ValueError('zero sell amount requires null ratio')


def verify(data, history_path):
    end = milliseconds(data['latest_closed_5m_end_utc'])
    start = end - 3600000
    deadline = time.monotonic() + 180
    candles, _ = request('https://data-api.binance.vision/api/v3/klines', {'symbol': 'BTCUSDT', 'interval': '1m', 'startTime': start, 'endTime': end - 1, 'limit': 1000}, deadline)
    if [int(c[0]) for c in candles] != list(range(start, end, 60000)):
        raise ValueError('independent Binance missing, duplicate or out-of-order candle')
    if any(int(c[6]) != int(c[0]) + 59999 or int(c[6]) >= end for c in candles):
        raise ValueError('independent Binance unclosed candle')
    for candle in candles:
        total, buy = Decimal(candle[7]), Decimal(candle[10])
        if not total.is_finite() or not buy.is_finite() or not 0 <= buy <= total:
            raise ValueError('invalid raw Binance amount')

    seen, cursors = {}, set()
    cursor = None
    oldest = newest = None
    duplicates = pages = 0
    for _ in range(80):
        params = {'limit': 1000}
        if cursor is not None:
            params['after'] = cursor
        trades, headers = request('https://api.exchange.coinbase.com/products/BTC-USD/trades', params, deadline)
        if not isinstance(trades, list) or not trades:
            raise ValueError('empty or invalid Coinbase page before full coverage')
        ids = [int(t['trade_id']) for t in trades]
        if ids != sorted(ids, reverse=True):
            raise ValueError('independent Coinbase page IDs are not descending')
        pages += 1
        for trade in trades:
            ident = int(trade['trade_id'])
            if ident in seen:
                if seen[ident] != trade:
                    raise ValueError('conflicting Coinbase duplicate')
                duplicates += 1
                continue
            seen[ident] = trade
            moment = milliseconds(trade['time'])
            oldest = moment if oldest is None else min(oldest, moment)
            newest = moment if newest is None else max(newest, moment)
            if start <= moment < end:
                price, size = Decimal(trade['price']), Decimal(trade['size'])
                if trade['side'] not in ('buy', 'sell') or not price.is_finite() or not size.is_finite() or price <= 0 or size < 0:
                    raise ValueError('invalid Coinbase maker side or amount')
        if oldest < start:
            break
        next_cursor = headers.get('cb-after')
        if not next_cursor or next_cursor in cursors or str(next_cursor) != str(ids[-1]):
            raise ValueError('Coinbase missing, repeated or incorrect pagination cursor')
        cursors.add(next_cursor)
        cursor = next_cursor
        time.sleep(.22)
    if oldest is None or oldest >= start or newest < end:
        raise ValueError('independent Coinbase snapshot does not cover entire hour')
    ordered = sorted(seen, reverse=True)
    if any(a - b != 1 for a, b in zip(ordered, ordered[1:])):
        raise ValueError('independent Coinbase trade ID continuity is unverified')
    with history_path.open(newline='') as handle:
        rows = {(r['exchange'], milliseconds(r['start_utc'])): r for r in csv.DictReader(handle)}
    latest = {r['exchange']: r for r in data['last_intervals']}
    relevant = [(milliseconds(t['time']), t) for t in seen.values() if start <= milliseconds(t['time']) < end]
    for slot in range(start, end, 300000):
        selected = [c for c in candles if slot <= int(c[0]) < slot + 300000]
        buy = sum((Decimal(c[10]) for c in selected), Decimal(0))
        total = sum((Decimal(c[7]) for c in selected), Decimal(0))
        compare(rows[('binance', slot)], buy, total - buy, sum(int(c[8]) for c in selected))
        if slot == end - 300000:
            compare(latest['binance'], buy, total - buy, sum(int(c[8]) for c in selected))
        selected = [t for moment, t in relevant if slot <= moment < slot + 300000]
        # Independent formula: REST side describes maker, so sell means taker buy.
        buy = sum((Decimal(t['price']) * Decimal(t['size']) for t in selected if t['side'] == 'sell'), Decimal(0))
        sell = sum((Decimal(t['price']) * Decimal(t['size']) for t in selected if t['side'] == 'buy'), Decimal(0))
        compare(rows[('coinbase', slot)], buy, sell, len(selected))
        if slot == end - 300000:
            compare(latest['coinbase'], buy, sell, len(selected))
    return {'status': 'PASS', 'verified_intervals': 24, 'binance_candles': len(candles), 'coinbase_pages': pages, 'coinbase_unique_trades': len(seen), 'coinbase_duplicates_seen': duplicates, 'coinbase_duplicates_deduplicated': True, 'coinbase_trade_id_gaps': 0, 'coinbase_maker_side_inverted': True, 'binance_quote_currency': 'USDT', 'coinbase_quote_currency': 'USD'}
