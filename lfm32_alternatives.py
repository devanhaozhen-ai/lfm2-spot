"""Official OKX public context. Never fills Binance fields or its evidence dimension.

API docs: https://www.okx.com/docs-v5/en/#rest-api-trading-data
No mirror, proxy, authentication, or orders. API errors remain explicit.
"""
from decimal import Decimal
from collector import utc
from lfm32_sources import decimal

BASE = 'https://www.okx.com'
INSTRUMENT = 'BTC-USDT-SWAP'
OI = '/api/v5/rubik/stat/contracts/open-interest-history'
FUNDING = '/api/v5/public/funding-rate-history'
FLOW = '/api/v5/rubik/stat/taker-volume-contract'


def request(client, path, params):
    response, _ = client.get(BASE+path, {'instId':INSTRUMENT, **params})
    if not isinstance(response,dict) or response.get('code') != '0' or not isinstance(response.get('data'),list):
        raise ValueError('OKX API error: '+str(response.get('code') if isinstance(response,dict) else 'invalid response'))
    return response['data']


def metadata(path):
    return {'exchange':'okx', 'instrument':INSTRUMENT, 'market_type':'linear_perpetual_swap',
            'source':BASE+path, 'quote_currency':'USDT', 'replaces_binance':False,
            'used_in_full_buy_evidence_score':False}


def history_index(raw, width):
    index = {}
    for row in raw:
        if len(row) != width: raise ValueError('OKX unexpected history row width')
        t = int(row[0])
        if t % 300000 or t in index: raise ValueError('OKX duplicate or misaligned timestamp')
        index[t] = [decimal(x) for x in row[1:]]
    return index


def fetch_oi(client, end):
    raw = request(client,OI,{'period':'5m','limit':100})
    index = history_index(raw,4)
    anchor = max((t for t in index if t <= end),default=None)
    if anchor is None: raise ValueError('OKX missing closed OI reference')
    changes = {}
    for m in (5,15,60):
        lower = anchor-m*60000; prior = index.get(lower)
        changes[str(m)+'m'] = {'start_utc':utc(lower), 'end_utc':utc(anchor),
                              'coverage':'complete' if prior else 'unavailable',
                              'delta_btc':float(index[anchor][1]-prior[1]) if prior else None,
                              'pct':float((index[anchor][1]/prior[1]-1)*100) if prior and prior[1] else None}
    return {**metadata(OI), 'raw':raw, 'as_of_utc':utc(anchor), 'unit':'BTC',
            'open_interest_btc':str(index[anchor][1]), 'open_interest_contracts':str(index[anchor][0]),
            'reported_notional_usd':str(index[anchor][2]), 'reported_notional_currency':'USD',
            'amount_conversion':'none; oiUsd is OKX-reported USD, not added to USDT', 'changes':changes}


def fetch_funding(client):
    raw = request(client,FUNDING,{'limit':2})
    if not raw: raise ValueError('OKX empty funding history')
    r = max(raw,key=lambda r:int(r['fundingTime']))
    if r['instId'] != INSTRUMENT: raise ValueError('OKX unexpected funding instrument')
    # fundingRate is predicted; realizedRate is the actual settled rate.
    rate = Decimal(r['realizedRate'])
    if not rate.is_finite(): raise ValueError('invalid realized funding rate')
    return {**metadata(FUNDING), 'raw':raw, 'rate':str(rate),
            'actual_funding_utc':utc(int(r['fundingTime'])), 'rate_kind':'last_settled',
            'source_rate_field':'realizedRate'}


def fetch_flow(client, start, end):
    # unit=0 explicitly asks for crypto/BTC, avoiding the default contracts unit.
    raw = request(client,FLOW,{'period':'5m','unit':'0','limit':100})
    index = history_index(raw,3)
    windows = {}
    for m in (5,15,60):
        lower = end-m*60000; expected = list(range(lower,end,300000))
        missing = [utc(t) for t in expected if t not in index]
        buy = sum((index[t][1] for t in expected),Decimal(0)) if not missing else None
        sell = sum((index[t][0] for t in expected),Decimal(0)) if not missing else None
        windows[str(m)+'m'] = {'start_utc':utc(lower), 'end_utc':utc(end),
                              'coverage':'complete' if not missing else 'unavailable', 'missing_intervals':missing,
                              'buy_btc':float(buy) if buy is not None else None,
                              'sell_btc':float(sell) if sell is not None else None,
                              'cvd_btc':float(buy-sell) if buy is not None else None}
    return {**metadata(FLOW), 'raw':raw, 'volume_currency':'BTC', 'api_unit':'0', 'source_granularity':'5m',
            'timestamp_basis':'bar opening timestamp; exclude current/unclosed bar', 'windows':windows,
            'cvd_definition':'window taker buy BTC minus taker sell BTC; no conversion to quote notional'}


def refresh_context(context, now_ms):
    """Same rules as primary evidence; cached metadata cannot refresh market time."""
    result = context
    for name,item in result.get('okx',{}).get('components',{}).items():
        reference = item.get('as_of_utc') if name == 'oi' else item.get('observed_utc')
        from collector import timestamp_ms
        age = (now_ms-timestamp_ms(reference))/1000 if reference else None
        fresh = bool(item.get('status') == 'available' and age is not None and -60 <= age <= 900)
        if name == 'flow' and item.get('windows'):
            lag = (now_ms-timestamp_ms(item['windows']['60m']['end_utc']))/1000
            fresh &= -60 <= lag <= 900
        if name == 'funding' and item.get('actual_funding_utc'):
            fresh &= timestamp_ms(item['actual_funding_utc']) <= now_ms+60000
        item['fresh_at_read'] = fresh
    return result
