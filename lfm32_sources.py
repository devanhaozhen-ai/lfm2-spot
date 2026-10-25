"""Official REST observations only. Each trade stream has a separate coverage proof."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from collector import utc, timestamp_ms

BINANCE = 'https://data-api.binance.vision/api/v3'
COINBASE = 'https://api.exchange.coinbase.com'
FUTURES = 'https://fapi.binance.com'
UNITS = {'binance': ('BTCUSDT', 'USDT'), 'coinbase': ('BTC-USD', 'USD')}


def decimal(value, positive=False):
    value = Decimal(str(value))
    if not value.is_finite() or value < 0 or (positive and value == 0):
        raise ValueError('invalid nonnegative source number')
    return value


class Client:
    def __init__(self, budget=100):
        self.deadline = time.monotonic() + budget
        self.audit = []

    def get(self, url, params=None):
        full = url + ('?' + urllib.parse.urlencode(params) if params else '')
        for attempt in range(3):
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('source budget exhausted')
            record = {'url': full, 'attempt': attempt + 1, 'requested_utc': utc(int(time.time()*1000))}
            self.audit.append(record)
            try:
                request = urllib.request.Request(full, headers={'User-Agent': 'LFM3.2-public-monitor', 'Accept': 'application/json', 'Cache-Control': 'no-cache'})
                with urllib.request.urlopen(request, timeout=min(15, remaining)) as response:
                    headers = {k.lower(): v for k, v in response.headers.items()}
                    record.update(status=response.status, received_utc=utc(int(time.time()*1000)),
                                  rate_headers={k: v for k, v in headers.items() if k.startswith('x-mbx-used-weight') or k in ('retry-after', 'cb-after', 'date')})
                    return json.load(response), headers
            except (urllib.error.URLError, OSError, TimeoutError, ValueError) as error:
                status = getattr(error, 'code', None)
                record.update(status=status, error=type(error).__name__)
                # A ban/restriction is not bypassed or repeatedly retried.
                if status in (403, 418, 451) or (status is not None and status not in (408, 429, 500, 502, 503, 504)) or attempt == 2:
                    raise
                delay = 2 ** attempt
                if status == 429:
                    try:
                        delay = max(delay, float(error.headers.get('Retry-After', 1)))
                    except (ValueError, TypeError):
                        pass
                if time.monotonic() + delay >= self.deadline or delay > 30:
                    raise TimeoutError('retry exceeds bounded budget') from error
                time.sleep(delay)


def normalize(exchange, raw):
    symbol, currency = UNITS[exchange]
    if exchange == 'binance':
        if type(raw['m']) is not bool:
            raise ValueError('Binance maker flag must be boolean')
        ident, moment, p, q = int(raw['a']), int(raw['T']), raw['p'], raw['q']
        side = 'sell' if raw['m'] else 'buy'
        first, last = int(raw['f']), int(raw['l'])
        if first > last:
            raise ValueError('reversed raw fill IDs')
        kind = 'aggregate_trade_same_taker_order_price_time'
    else:
        if raw['side'] not in ('buy', 'sell'):
            raise ValueError('invalid Coinbase maker side')
        ident, moment, p, q = int(raw['trade_id']), timestamp_ms(raw['time']), raw['price'], raw['size']
        side = 'buy' if raw['side'] == 'sell' else 'sell'
        first = last = ident
        kind = 'individual_match'
    p, q = decimal(p, True), decimal(q, True)
    return {'exchange': exchange, 'symbol': symbol, 'quote_currency': currency,
            'trade_id': ident, 'timestamp_ms': moment, 'time_utc': utc(moment),
            'price': str(p), 'quantity_btc': str(q), 'notional_quote': str(p*q),
            'taker_side': side, 'trade_kind': kind, 'first_fill_id': first, 'last_fill_id': last,
            'taker_order_id': None, 'unique_taker_orders': None, 'source_record': raw,
            'source': BINANCE + '/aggTrades' if exchange == 'binance' else COINBASE + '/products/BTC-USD/trades'}


def fetch_trades(exchange, start, end, client, max_pages=120):
    """Prove both temporal brackets AND ID continuity, otherwise no full-window sums.

    Binance agg IDs and raw fill ranges are distinct; never mix raw and agg feeds.
    Coinbase cb-after cursor walks backwards, side denotes the maker.
    """
    seen, cursor, cursors = {}, None, set()
    reason = 'page_limit_reached'
    for page in range(max_pages):
        if exchange == 'binance':
            params = {'symbol': 'BTCUSDT', 'limit': 1000}
            params.update({'startTime': start - 60000} if cursor is None else {'fromId': cursor})
            raw, headers = client.get(BINANCE + '/aggTrades', params)
        else:
            params = {'limit': 1000}
            if cursor is not None:
                params['after'] = cursor
            raw, headers = client.get(COINBASE + '/products/BTC-USD/trades', params)
        if not isinstance(raw, list) or not raw:
            reason = 'empty_or_invalid_page'
            break
        trades = [normalize(exchange, t) for t in raw]
        ids = [t['trade_id'] for t in trades]
        if ids != sorted(ids, reverse=exchange == 'coinbase') or len(ids) != len(set(ids)):
            raise ValueError('page IDs not strictly ordered')
        for t in trades:
            ident = t['trade_id']
            if ident in seen and seen[ident] != t:
                raise ValueError('conflicting duplicate trade')
            seen[ident] = t
        moments = [t['timestamp_ms'] for t in seen.values()]
        if min(moments) < start and max(moments) >= end:
            reason = 'temporal_brackets_reached'
            break
        next_cursor = ids[-1] + 1 if exchange == 'binance' else headers.get('cb-after')
        if next_cursor is None or (exchange == 'coinbase' and str(next_cursor) != str(ids[-1])) or str(next_cursor) in cursors:
            reason = 'missing_or_repeated_cursor'
            break
        cursors.add(str(next_cursor))
        cursor = next_cursor
        time.sleep(.12 if exchange == 'binance' else .22)
    ordered = sorted(seen.values(), key=lambda t: t['trade_id'])
    gaps = sum(b['trade_id'] != a['trade_id'] + 1 for a, b in zip(ordered, ordered[1:]))
    reversed_time = any(b['timestamp_ms'] < a['timestamp_ms'] for a, b in zip(ordered, ordered[1:]))
    fill_gaps = exchange == 'binance' and any(b['first_fill_id'] != a['last_fill_id'] + 1 for a, b in zip(ordered, ordered[1:]))
    oldest = min((t['timestamp_ms'] for t in ordered), default=None)
    newest = max((t['timestamp_ms'] for t in ordered), default=None)
    return {'trades': ordered, 'coverage': 'complete' if reason == 'temporal_brackets_reached' and not gaps and not reversed_time and not fill_gaps else 'incomplete',
            'reason': reason, 'id_gaps': gaps, 'fill_id_gaps': bool(fill_gaps), 'reversed_time': reversed_time,
            'pages': page + 1, 'oldest_ms': oldest, 'newest_ms': newest,
            'requested_start_utc': utc(start), 'requested_end_utc': utc(end)}


def book_snapshot(exchange, client, wall_threshold):
    url = BINANCE + '/depth' if exchange == 'binance' else COINBASE + '/products/BTC-USD/book'
    raw, _ = client.get(url, {'symbol': 'BTCUSDT', 'limit': 10} if exchange == 'binance' else {'level': 2})
    levels = {}
    for side in ('bids', 'asks'):
        selected = raw[side][:10]
        levels[side] = [{'price': str(decimal(r[0], True)), 'quantity_btc': str(decimal(r[1], True)),
                         'notional_quote': str(decimal(r[0], True)*decimal(r[1], True))} for r in selected]
        prices = [Decimal(r['price']) for r in levels[side]]
        if len(prices) != 10 or len(set(prices)) != 10 or prices != sorted(prices, reverse=side == 'bids'):
            raise ValueError('invalid top-ten depth ordering')
    if Decimal(levels['bids'][0]['price']) >= Decimal(levels['asks'][0]['price']):
        raise ValueError('crossed order book')
    bid, ask = (sum(Decimal(r['quantity_btc']) for r in levels[s]) for s in ('bids', 'asks'))
    return {'raw': raw, 'status': 'snapshot_only', 'source': url, 'exchange': exchange,
            'quote_currency': UNITS[exchange][1], 'observed_utc': utc(int(time.time()*1000)),
            'exchange_event_utc': None, 'sequence': raw.get('lastUpdateId', raw.get('sequence')),
            **levels, 'imbalance_btc': float((bid-ask)/(bid+ask)),
            'walls': {s: [r for r in levels[s] if Decimal(r['notional_quote']) >= wall_threshold] for s in levels},
            'sequence_continuity_verified': False, 'additions': None, 'cancellations': None,
            'executed_changes': None, 'liquidity_disappearance': None, 'iceberg_inference': None,
            'passive_absorption_inference': None,
            'limitation': 'REST snapshot only; displayed orders are not executions or institutional buying. Incremental analysis unavailable in phase 1.'}


def fetch_oi(client, end):
    raw, _ = client.get(FUTURES + '/futures/data/openInterestHist', {'symbol': 'BTCUSDT', 'period': '5m', 'startTime': end-3900000, 'endTime': end, 'limit': 30})
    index = {}
    for r in raw:
        t = int(r['timestamp'])
        if r['symbol'] != 'BTCUSDT' or t % 300000 or t in index or not end-3900000 <= t <= end:
            raise ValueError('invalid OI history timestamp/symbol')
        index[t] = r
        decimal(r['sumOpenInterest'])
        decimal(r['sumOpenInterestValue'])
    anchor = max(index, default=None)
    if anchor is None:
        raise ValueError('empty OI history')
    base = decimal(index[anchor]['sumOpenInterest'])
    changes = {}
    for m in (5, 15, 60):
        lower = anchor-m*60000
        prior = decimal(index[lower]['sumOpenInterest']) if lower in index else None
        changes[str(m)+'m'] = {'start_utc': utc(lower), 'end_utc': utc(anchor), 'delta_btc': float(base-prior) if prior is not None else None,
                              'pct': float((base/prior-1)*100) if prior else None, 'coverage': 'complete' if prior is not None else 'unavailable'}
    return {'source': FUTURES+'/futures/data/openInterestHist', 'unit': 'BTC', 'as_of_utc': utc(anchor),
            'open_interest_btc': str(base), 'notional_quote': str(decimal(index[anchor]['sumOpenInterestValue'])),
            'quote_currency': 'USDT', 'changes': changes, 'raw': raw}


def fetch_funding(client):
    settled, _ = client.get(FUTURES + '/fapi/v1/fundingRate', {'symbol': 'BTCUSDT', 'limit': 1})
    r = settled[-1]
    rate = Decimal(str(r['fundingRate']))
    if not rate.is_finite() or r['symbol'] != 'BTCUSDT':
        raise ValueError('invalid settled funding')
    return {'source': FUTURES+'/fapi/v1/fundingRate', 'rate': str(rate),
            'actual_funding_utc': utc(int(r['fundingTime'])), 'rate_kind': 'last_settled', 'raw': settled}


def fetch_futures_flow(client, start, end):
    raw, _ = client.get(FUTURES + '/fapi/v1/klines', {'symbol': 'BTCUSDT', 'interval': '1m', 'startTime': start, 'endTime': end-1, 'limit': 1000})
    index = {}
    for r in raw:
        t = int(r[0])
        if t in index or not start <= t < end or t % 60000 or int(r[6]) != t+59999:
            raise ValueError('invalid futures candle timestamp')
        total, buy = decimal(r[7]), decimal(r[10])
        if buy > total:
            raise ValueError('futures buy exceeds total')
        index[t] = (buy, total-buy, decimal(r[4], True))
    windows = {}
    for m in (5, 15, 60):
        lower = end-m*60000
        complete = all(t in index for t in range(lower, end, 60000))
        b = sum((index[t][0] for t in range(lower, end, 60000)), Decimal(0)) if complete else None
        s = sum((index[t][1] for t in range(lower, end, 60000)), Decimal(0)) if complete else None
        windows[str(m)+'m'] = {'coverage': 'complete' if complete else 'unavailable', 'start_utc': utc(lower), 'end_utc': utc(end),
                              'buy_quote': float(b) if complete else None, 'sell_quote': float(s) if complete else None, 'cvd_quote': float(b-s) if complete else None}
    return {'source': FUTURES+'/fapi/v1/klines', 'quote_currency': 'USDT', 'windows': windows,
            'cvd_definition': 'window taker-buy minus taker-sell quote volume; not cumulative across gaps', 'raw': raw}
