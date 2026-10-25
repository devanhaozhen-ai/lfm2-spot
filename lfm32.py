"""LFM3.2 phase 1: bounded REST windows; no realtime or institutional claims."""
import argparse
import copy
import gzip
import hashlib
import json
import math
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from collector import atomic_write, utc, timestamp_ms
from lfm32_sources import (Client, UNITS, fetch_trades, book_snapshot, fetch_oi,
                           fetch_funding, fetch_futures_flow)

ROOT = Path(__file__).parent
METRICS = ('buy_quote', 'sell_quote', 'large_buy_quote', 'large_sell_quote', 'large_net_quote',
           'cvd_quote', 'large_share', 'trade_count', 'large_count', 'large_buy_count', 'large_sell_count')


def load_config(path=ROOT/'lfm32_config.json'):
    config = json.loads(Path(path).read_text())
    thresholds = config['large_thresholds_quote']
    if len(thresholds) != 4 or thresholds != sorted(set(thresholds)) or any(not math.isfinite(x) or x <= 0 for x in thresholds):
        raise ValueError('four strictly increasing positive large thresholds required')
    if not 0 < config['dynamic_quantile'] < 1 or config['dynamic_min_samples'] < 1:
        raise ValueError('invalid dynamic quantile configuration')
    if config['weights'] != {'spot_large_flow': 30, 'exchange_agreement': 25, 'price_volume': 20, 'book_absorption': 15, 'derivatives': 10}:
        if set(config['weights']) != {'spot_large_flow', 'exchange_agreement', 'price_volume', 'book_absorption', 'derivatives'} or sum(config['weights'].values()) != 100 or min(config['weights'].values()) < 0:
            raise ValueError('weights must be nonnegative and sum to 100')
    for key, lower, upper in [('max_pages',1,200), ('source_budget_seconds',1,120), ('consecutive_count',2,100), ('consecutive_max_gap_ms',1,60000)]:
        if not lower <= config[key] <= upper:
            raise ValueError('invalid bounded setting: '+key)
    return config


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def covered(stream, start, end):
    rows = stream.get('trades', [])
    return bool(rows and not stream.get('id_gaps') and not stream.get('fill_id_gaps') and not stream.get('reversed_time') and
                rows[0]['timestamp_ms'] < start and rows[-1]['timestamp_ms'] >= end)


def aggregate(stream, start, end, config, threshold=None):
    result = {'start_utc': utc(start), 'end_utc': utc(end), 'coverage': 'complete' if covered(stream, start, end) else 'unavailable',
              'count_unit': 'feed_records; Binance aggregate groups, Coinbase individual matches; NOT unique orders',
              **dict.fromkeys(METRICS), 'tiers': None, 'consecutive_events': None, 'large_cvd_series': None}
    if result['coverage'] != 'complete':
        result['reason'] = stream.get('error', stream.get('reason', 'no verified source coverage'))
        return result
    rows = [r for r in stream['trades'] if start <= r['timestamp_ms'] < end]
    limits = [Decimal(str(v)) for v in config['large_thresholds_quote']]
    limit = Decimal(str(threshold)) if threshold is not None else limits[0]
    buy = sell = lb = ls = Decimal(0)
    bc = sc = 0
    tiers = [{'min_inclusive_quote': float(v), 'max_exclusive_quote': float(limits[i+1]) if i < 3 else None,
              'buy_quote': 0., 'sell_quote': 0., 'buy_count': 0, 'sell_count': 0} for i, v in enumerate(limits)]
    running, events, streak = Decimal(0), [], []
    buckets = {t: Decimal(0) for t in range(start, end, 300000)}
    def flush():
        if len(streak) >= config['consecutive_count']:
            events.append({'taker_side': streak[0]['taker_side'], 'start_utc': streak[0]['time_utc'], 'end_utc': streak[-1]['time_utc'],
                           'record_count': len(streak), 'notional_quote': float(sum((Decimal(r['notional_quote']) for r in streak), Decimal(0))),
                           'first_id': streak[0]['trade_id'], 'last_id': streak[-1]['trade_id']})
    for r in rows:
        n = Decimal(r['notional_quote'])
        side = r['taker_side']
        if side == 'buy': buy += n
        else: sell += n
        for i, v in reversed(list(enumerate(limits))):
            if n >= v:
                tiers[i][side+'_quote'] += float(n)
                tiers[i][side+'_count'] += 1
                break
        if n >= limit:
            if side == 'buy': lb += n; bc += 1
            else: ls += n; sc += 1
            buckets[start + ((r['timestamp_ms']-start)//300000)*300000] += n if side == 'buy' else -n
            if streak and (streak[-1]['taker_side'] != side or r['timestamp_ms']-streak[-1]['timestamp_ms'] > config['consecutive_max_gap_ms']):
                flush(); streak = []
            streak.append(r)
        else:
            flush(); streak = []
    flush()
    series = []
    for t, net in buckets.items():
        running += net
        series.append({'end_utc': utc(min(t+300000, end)), 'net_quote': float(net), 'cumulative_quote': float(running)})
    result.update(buy_quote=float(buy), sell_quote=float(sell), large_buy_quote=float(lb), large_sell_quote=float(ls),
                  large_net_quote=float(lb-ls), cvd_quote=float(buy-sell), large_share=float((lb+ls)/(buy+sell)) if buy+sell else None,
                  trade_count=len(rows), large_count=bc+sc, large_buy_count=bc, large_sell_count=sc, tiers=tiers,
                  consecutive_events=events, large_cvd_series=series, large_cvd_anchor_utc=utc(start),
                  large_cvd_definition='sum of large taker-buy minus large taker-sell since explicit window anchor; no cross-gap continuation',
                  large_threshold_quote=float(limit), first_trade_id=rows[0]['trade_id'] if rows else None,
                  last_trade_id=rows[-1]['trade_id'] if rows else None)
    # Trade-based price/volume observations remain distinct from candle volume.
    result['price_change_pct'] = float((Decimal(rows[-1]['price'])/Decimal(rows[0]['price'])-1)*100) if len(rows) > 1 else None
    result['volume_btc'] = float(sum((Decimal(r['quantity_btc']) for r in rows), Decimal(0)))
    return result


def build_market(exchange, stream, start, end, config):
    symbol, currency = UNITS[exchange]
    sample = sorted(Decimal(r['notional_quote']) for r in stream.get('trades', []) if start <= r['timestamp_ms'] < end)
    q = config['dynamic_quantile']
    threshold = sample[math.ceil(len(sample)*q)-1] if covered(stream, start, end) and len(sample) >= config['dynamic_min_samples'] else None
    rows = stream.get('trades', [])
    latest = rows[-1] if rows else None
    return {'exchange': exchange, 'symbol': symbol, 'quote_currency': currency,
            'amount_conversion': 'none; thresholds are native quote units, USDT is not assumed USD',
            'latest_price': latest['price'] if latest else None, 'latest_trade_utc': latest['time_utc'] if latest else None,
            'trade_kind': latest['trade_kind'] if latest else None,
            'windows': {str(m)+'m': aggregate(stream, end-m*60000, end, config) for m in (5,15,60)},
            'dynamic': {'quantile': q, 'threshold_quote': float(threshold) if threshold is not None else None,
                        'sample_count': len(sample), 'calibration_start_utc': utc(start), 'calibration_end_utc': utc(end),
                        'method': 'nearest-rank empirical quantile of same-window feed records; descriptive in-sample, not predictive',
                        'windows': {str(m)+'m': aggregate(stream, end-m*60000, end, config, threshold) for m in (5,15,60)} if threshold is not None else None},
            'coverage_proof': {k:v for k,v in stream.items() if k != 'trades'}}


def sign(value):
    return (value > 0) - (value < 0)


def refresh_quality(source, now=None):
    """Always use at READ time; stale snapshots lose scores and classifications."""
    d = copy.deepcopy(source)
    now_ms = int((now or datetime.now(timezone.utc)).timestamp()*1000)
    def age(value):
        return (now_ms-timestamp_ms(value))/1000 if value else None
    d['quality_evaluated_utc'] = utc(now_ms)
    d['data_age_seconds'] = age(d['generated_utc'])
    d['market_data_age_seconds'] = age(d['window_end_utc'])
    fresh_generation = all(-60 <= v <= 900 for v in (d['data_age_seconds'], d['market_data_age_seconds']))
    for m in d['markets'].values():
        m['latest_trade_age_seconds'] = age(m['latest_trade_utc'])
        m['monitoring_eligible'] = bool(fresh_generation and m['latest_trade_age_seconds'] is not None and -60 <= m['latest_trade_age_seconds'] <= 900 and all(w['coverage'] == 'complete' for w in m['windows'].values()))
    d['monitoring_eligible'] = all(m['monitoring_eligible'] for m in d['markets'].values())
    d['quality_status'] = 'stale' if not fresh_generation else ('partial' if d.get('errors') else 'ok') if d['monitoring_eligible'] else 'incomplete'
    d['monitoring_scope'] = 'verified fresh 5m/15m/60m spot trade windows; optional book/derivatives coverage is reported separately'
    d['full_evidence_eligible'] = False  # Phase 1 cannot verify continuous book absorption.
    d['stale_after_seconds'] = 900
    # A new file timestamp cannot refresh old derivatives or book evidence.
    for book in d.get('order_books', {}).values():
        a = age(book.get('observed_utc'))
        book['fresh_at_read'] = bool(a is not None and -60 <= a <= 900)
    derivatives = d.get('derivatives', {})
    for name, item in derivatives.items():
        evidence_time = item.get('as_of_utc') if name == 'oi' else item.get('observed_utc')
        a = age(evidence_time)
        item['fresh_at_read'] = bool(item.get('status') == 'available' and a is not None and -60 <= a <= 900)
        if name == 'flow' and item.get('windows'):
            a = age(item['windows']['60m']['end_utc'])
            item['fresh_at_read'] &= bool(a is not None and -60 <= a <= 900)
        if name == 'funding' and item.get('actual_funding_utc'):
            item['fresh_at_read'] &= age(item['actual_funding_utc']) >= -60
    d['analysis'] = analyze(d)
    return d


def analyze(data):
    markets, weights = data['markets'], data['config']['weights']
    usable = data['monitoring_eligible']
    dimensions = {k: {'weight': v, 'value': None, 'reason': 'unavailable'} for k,v in weights.items()}
    agreement = {'direction': 'unavailable', 'amount_aggregation': 'disabled; USDT and USD are not added'}
    signals, explanations = [], []
    if usable:
        b, c = (markets[e]['windows']['15m'] for e in ('binance','coinbase'))
        ratios = [w['large_net_quote']/(w['large_buy_quote']+w['large_sell_quote']) for w in (b,c) if w['large_buy_quote']+w['large_sell_quote']]
        if ratios:
            dimensions['spot_large_flow'].update(value=sum((r+1)/2 for r in ratios)/len(ratios), reason='mean per-venue large flow imbalance mapped [-1,1] to [0,1]; no currency addition')
        sb, sc = sign(b['large_net_quote']), sign(c['large_net_quote'])
        agreement['spot_cvd_direction'] = 'buy' if b['cvd_quote'] > 0 and c['cvd_quote'] > 0 else 'sell' if b['cvd_quote'] < 0 and c['cvd_quote'] < 0 else 'conflict' if b['cvd_quote']*c['cvd_quote'] < 0 else 'neutral_or_one_sided'
        agreement['per_venue'] = {e: {'large_flow_sign': sign(markets[e]['windows']['15m']['large_net_quote']), 'all_flow_sign': sign(markets[e]['windows']['15m']['cvd_quote'])} for e in ('binance','coinbase')}
        agreement['direction'] = 'buy' if sb == sc == 1 else 'sell' if sb == sc == -1 else 'conflict' if sb*sc < 0 else 'neutral_or_one_sided'
        dimensions['exchange_agreement'].update(value=1 if sb==sc==1 else 0 if sb==sc==-1 else .5, reason='15m per-venue native-quote large flow signs; neutral/conflict=0.5')
        pv = []
        for e in ('binance','coinbase'):
            w5, w15 = markets[e]['windows']['5m'], markets[e]['windows']['15m']
            prior_mean = (w15['volume_btc']-w5['volume_btc'])/2
            if w15.get('price_change_pct') is not None and prior_mean > 0:
                pv.append(1 if w15['price_change_pct'] > 0 and w5['volume_btc'] > prior_mean else 0 if w15['price_change_pct'] < 0 and w5['volume_btc'] > prior_mean else .5)
        if len(pv) == 2:
            dimensions['price_volume'].update(value=sum(pv)/2, reason='15m trade price direction and last5m BTC volume vs preceding two 5m mean')
        if agreement['direction'] == 'buy': signals.append('疑似大资金主动买入')
        elif agreement['direction'] == 'sell': signals.append('疑似大资金主动卖出')
        elif agreement['direction'] == 'conflict': signals.append('信号冲突')
        if agreement['spot_cvd_direction'] == 'conflict' and '信号冲突' not in signals: signals.append('信号冲突')
        der = data.get('derivatives', {})
        oi, funding, flow = (der.get(k,{}) for k in ('oi','funding','flow'))
        if oi.get('fresh_at_read') and oi.get('as_of_utc') == data['window_end_utc'] and oi['changes']['15m']['coverage'] == 'complete':
            p, delta = b.get('price_change_pct'), oi['changes']['15m']['delta_btc']
            if p is not None and delta is not None:
                if p > 0 and delta > 0: explanations.append('价格上涨、OI增加；现货主动买入若同时增加，构成条件性买入证据，无法确定合约开仓方向。')
                if p > 0 and delta < 0: signals.append('疑似挤空'); explanations.append('价格上涨、OI下降可能涉及空头回补；无法单凭二者确认。')
                if p < 0 and delta > 0: explanations.append('价格下跌、OI增加可能涉及新增空头，也可能为对手方新增多头，方向不能确定。')
                if p < 0 and delta < 0: signals.append('疑似多头踩踏'); explanations.append('价格下跌、OI下降可能涉及多头平仓；未获得清算逐笔证据。')
            if funding.get('fresh_at_read') and p is not None:
                rate = Decimal(funding['rate'])
                dimensions['derivatives'].update(value=1 if p > 0 and delta > 0 and b['cvd_quote'] > 0 and rate <= 0 else 0 if p < 0 and delta > 0 and b['cvd_quote'] < 0 and rate >= 0 else .5, reason='conditional price/OI/spot CVD and last settled funding confirmation; no position identity inference')
        if flow.get('fresh_at_read') and flow['windows']['15m']['coverage'] == 'complete' and b['cvd_quote']*flow['windows']['15m']['cvd_quote'] < 0:
            signals.append('现货与合约背离')
    dimensions['book_absorption']['reason'] = 'REST snapshots cannot verify absorption, cancellations, disappearing liquidity or iceberg orders; phase 2 required'
    active = [k for k,v in dimensions.items() if v['value'] is not None]
    missing = [k for k in dimensions if k not in active]
    available_weight = sum(weights[k] for k in active)
    points = sum(dimensions[k]['value']*weights[k] for k in active)
    if not usable or not signals: signals.append('数据不足，无法判断')
    return {'signals': signals, 'conditional_explanations': explanations, 'exchange_agreement': agreement,
            'buy_evidence_score_0_100': round(points,3) if not missing and usable else None,
            'observed_evidence_points': round(points,3) if active else None, 'available_weight': available_weight,
            'effective_dimensions': active, 'missing_dimensions': missing, 'dimensions': dimensions,
            'confidence': 'limited_unvalidated' if usable else 'insufficient', 'predictive_use_eligible': False,
            'calibration': 'initial heuristic weights; NOT institutional-entry probability. No historical backtest or out-of-sample validation yet. Missing dimensions never receive zero or rescaled 100-point conclusions.'}


def persist_history(path, incoming):
    previous = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    def key(r): return r['exchange'], r['start_utc'], r['config_hash']
    index = {key(r): r for r in previous}
    if len(index) != len(previous): raise ValueError('duplicate LFM3.2 historical key')
    for row in incoming:
        k = key(row)
        if k not in index or index[k]['coverage'] != 'complete': index[k] = row
    rows = sorted(index.values(), key=lambda r:(r['start_utc'], r['exchange'], r['config_hash']))
    atomic_write(path, lambda h: h.write(''.join(json.dumps(r, ensure_ascii=False, separators=(',', ':'), allow_nan=False)+'\n' for r in rows)))


def collect(data_dir=ROOT/'data', config=None):
    config = config or load_config()
    data_dir = Path(data_dir); data_dir.mkdir(parents=True, exist_ok=True)
    started = int(time.time()*1000); end = started//300000*300000; start = end-3600000
    markets, books, errors, audit, raw, streams = {}, {}, {}, {}, {}, {}
    for e in ('binance', 'coinbase'):
        client = Client(config['source_budget_seconds'])
        try: stream = fetch_trades(e, start, end, client, config['max_pages'])
        except Exception as error:
            stream = {'trades': [], 'coverage': 'unavailable', 'error': str(error)}
        streams[e] = stream
        raw[e] = stream
        if not covered(stream,start,end): errors[e+'_trades'] = stream.get('error', stream.get('reason'))
        audit[e+'_trades'] = client.audit
        markets[e] = build_market(e, stream, start, end, config)
        client = Client(20)
        try:
            books[e] = book_snapshot(e, client, Decimal(str(config['wall_threshold_quote'])))
            raw[e+'_book'] = books[e].pop('raw')
        except Exception as error:
            errors[e+'_book'] = str(error); books[e] = {'status':'unavailable', 'error': str(error)}
        audit[e+'_book'] = client.audit
    derivatives = {}
    for name, fn in [('oi', lambda c: fetch_oi(c,end)), ('funding', fetch_funding), ('flow', lambda c: fetch_futures_flow(c,start,end))]:
        client = Client(25)
        try:
            item = fn(client); raw['futures_'+name] = item.pop('raw')
            derivatives[name] = {'status':'available', 'observed_utc':utc(int(time.time()*1000)), **item}
        except Exception as error:
            errors['futures_'+name] = str(error); derivatives[name] = {'status':'unavailable', 'error':str(error)}
        audit['futures_'+name] = client.audit
    # Reproducible normalized source archive retained as an Actions artifact, not bloating git history.
    raw.update(config=config, requested_start_utc=utc(start), requested_end_utc=utc(end), source_audit=audit)
    raw_bytes = json.dumps(raw, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    archive = gzip.compress(raw_bytes, mtime=0)
    (data_dir/'lfm32_raw.json.gz').write_bytes(archive)
    out = {'schema_version':'3.2', 'phase':1, 'collection_type':'bounded_rest_polling_not_realtime',
           'generated_utc':utc(int(time.time()*1000)), 'collection_started_utc':utc(started),
           'window_start_utc':utc(start), 'window_end_utc':utc(end),
           'collection_mode':os.environ.get('LFM2_COLLECTION_MODE','manual_workflow_dispatch'),
           'collection_run_id':os.environ.get('GITHUB_RUN_ID'), 'request_id':os.environ.get('LFM2_REQUEST_ID',''),
           'markets':markets, 'order_books':books, 'derivatives':derivatives, 'errors':errors, 'source_audit':audit,
           'config':config, 'config_hash':digest(config), 'cross_exchange_notional_aggregation':'disabled',
           'raw_archive':{'file':'lfm32_raw.json.gz', 'sha256':hashlib.sha256(archive).hexdigest(),
                          'storage':'Actions artifact lfm32-source-<run_id>, 30 days retention; local file when run locally',
                          'raw_format':'normalized lossless trade fields, boundary trades, original derivatives responses, request audit'},
           'limitations':['No institutional identity inferred.', 'REST snapshot book is not a continuous sequence-verified order book.',
                         'Binance aggregate groups are not individual fills or complete unique taker orders; large-group classification cannot prove a large single fill.',
                         'USD and USDT never added; native quote thresholds do not assert FX parity.',
                         'Phase 1 has no liquidation feed; heatmaps must carry their own source and observation time.']}
    out = refresh_quality(out)
    incoming = []
    for e in markets:
        for t in range(start,end,300000):
            row = aggregate(streams[e],t,t+300000,config)
            incoming.append({'exchange':e, 'quote_currency':UNITS[e][1], 'config_hash':out['config_hash'], 'source_archive_sha256':out['raw_archive']['sha256'], **row})
    persist_history(data_dir/'lfm32_history.jsonl',incoming)
    atomic_write(data_dir/'lfm32_latest.json',lambda h:h.write(json.dumps(out,ensure_ascii=False,indent=2,allow_nan=False)+'\n'))
    summary = copy.deepcopy(out)
    summary['schema_version'] = '3.2-summary'
    summary.pop('source_audit')
    for m in summary['markets'].values():
        for w in m['windows'].values(): w.pop('consecutive_events',None)
        m['dynamic'].pop('windows',None)
    for b in summary['order_books'].values():
        b.pop('bids',None); b.pop('asks',None)
    atomic_write(data_dir/'lfm32_summary.json',lambda h:h.write(json.dumps(summary,ensure_ascii=False,separators=(',', ':'),allow_nan=False)+'\n'))
    print(json.dumps({'lfm32':'published', 'quality_status':out['quality_status'], 'errors':errors, 'raw_sha256':out['raw_archive']['sha256']},ensure_ascii=False))
    return out


def main():
    p = argparse.ArgumentParser(); p.add_argument('--data-dir',type=Path,default=ROOT/'data'); p.add_argument('--read',type=Path)
    args = p.parse_args()
    if args.read:
        print(json.dumps(refresh_quality(json.loads(args.read.read_text())),ensure_ascii=False,indent=2,allow_nan=False))
    else: collect(args.data_dir)


if __name__ == '__main__': main()
