"""Refetch seven days directly, independently of the LFM3 collector/parser."""
import csv
import math
import time
from decimal import Decimal
from quality import parse_time
from verify_sources import request, compare


def verify(data, cvd_path):
    deadline = time.monotonic() + 180
    with open(cvd_path, newline='') as handle:
        cvd = {(r['exchange'], int(parse_time(r['start_utc']).timestamp())): r for r in csv.DictReader(handle)}
    result = {'status': 'PASS', 'candles_verified': {}, 'binance_cvd_intervals_verified': 0}
    for exchange in ('binance', 'coinbase'):
        result['candles_verified'][exchange] = {}
        for frame, step in (('5m',300), ('1h',3600)):
            item = data['markets'][exchange][frame]
            lower, end = int(parse_time(item['start_utc']).timestamp()), int(parse_time(item['end_utc']).timestamp())
            observed = {}
            page_size = 1000 if exchange == 'binance' else 299
            for start in range(lower, end, step * page_size):
                upper = min(end, start + step * page_size)
                if exchange == 'binance':
                    params = {'symbol':'BTCUSDT', 'interval':frame, 'startTime':start*1000, 'endTime':upper*1000-1, 'limit':1000}
                    url = 'https://data-api.binance.vision/api/v3/klines'
                else:
                    from datetime import datetime, timezone
                    params = {'granularity':step, 'start':datetime.fromtimestamp(start,timezone.utc).isoformat(), 'end':datetime.fromtimestamp(upper,timezone.utc).isoformat()}
                    url = 'https://api.exchange.coinbase.com/products/BTC-USD/candles'
                raw, _ = request(url, params, deadline)
                for c in raw:
                    opened = int(c[0]) // 1000 if exchange == 'binance' else int(c[0])
                    if not start <= opened < upper:
                        continue
                    if opened in observed:
                        raise ValueError('duplicate independent historical candle')
                    if exchange == 'binance' and int(c[6]) != (opened + step)*1000-1:
                        raise ValueError('independent historical Binance candle not closed')
                    values = (c[1],c[2],c[3],c[4],c[5]) if exchange == 'binance' else (c[3],c[2],c[1],c[4],c[5])
                    observed[opened] = values
                    if exchange == 'binance' and frame == '5m':
                        buy, total = Decimal(c[10]), Decimal(c[7])
                        if not 0 <= buy <= total:
                            raise ValueError('invalid independent historical taker amount')
                        compare(cvd[('binance',opened)], buy, total-buy, int(c[8]))
                        result['binance_cvd_intervals_verified'] += 1
                time.sleep(.12)
            expected = list(range(lower,end,step))
            if sorted(observed) != expected:
                raise ValueError('independent seven-day ' + exchange + ' ' + frame + ' coverage failed')
            for candle in item['candles']:
                opened = int(parse_time(candle['start_utc']).timestamp())
                for name, value in zip(('open','high','low','close','volume_btc'), observed[opened]):
                    if not math.isclose(float(candle[name]), float(value), rel_tol=1e-12, abs_tol=1e-8):
                        raise ValueError('independent historical ' + name + ' differs')
            result['candles_verified'][exchange][frame] = len(observed)
    return result
