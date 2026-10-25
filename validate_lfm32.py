"""Independent source-archive, arithmetic, coverage and read-time contract checks."""
import argparse
import gzip
import hashlib
import json
import math
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from collector import timestamp_ms
from lfm32 import refresh_quality


def equal(actual, expected, label):
    if actual is None or not math.isfinite(float(actual)) or not math.isclose(float(actual),float(expected),rel_tol=1e-10,abs_tol=1e-6):
        raise ValueError('independent '+label+' mismatch')


def validate(directory, require_usable=False, now=None):
    directory = Path(directory)
    d = json.loads((directory/'lfm32_latest.json').read_text())
    if d['schema_version'] != '3.2' or d['collection_mode'] not in ('scheduled','manual_workflow_dispatch'):
        raise ValueError('invalid schema or collection mode')
    if d['cross_exchange_notional_aggregation'] != 'disabled': raise ValueError('unconverted currency aggregation')
    archive = (directory/'lfm32_raw.json.gz').read_bytes()
    if hashlib.sha256(archive).hexdigest() != d['raw_archive']['sha256']: raise ValueError('archive SHA256 mismatch')
    raw = json.loads(gzip.decompress(archive))
    start, end = timestamp_ms(d['window_start_utc']), timestamp_ms(d['window_end_utc'])
    if start % 300000 or end-start != 3600000: raise ValueError('invalid time range')
    for e, currency in [('binance','USDT'),('coinbase','USD')]:
        market, stream = d['markets'][e], raw[e]
        if market['quote_currency'] != currency: raise ValueError('wrong quote currency')
        trades = stream.get('trades',[])
        ids = [r['trade_id'] for r in trades]
        if ids != sorted(set(ids)): raise ValueError('duplicate/unsorted source IDs')
        for r in trades:
            if r['exchange'] != e or r['quote_currency'] != currency or r['taker_side'] not in ('buy','sell'): raise ValueError('invalid source metadata')
            if timestamp_ms(r['time_utc']) != r['timestamp_ms']: raise ValueError('trade time mismatch')
            equal(r['notional_quote'],Decimal(r['price'])*Decimal(r['quantity_btc']),'trade amount')
            original = r['source_record']
            expected_side = ('sell' if original['m'] else 'buy') if e == 'binance' else ('buy' if original['side'] == 'sell' else 'sell')
            if r['taker_side'] != expected_side: raise ValueError('maker/taker direction mismatch')
        for frame in ('5m','15m','60m'):
            w = market['windows'][frame]
            lower = end-int(frame[:-1])*60000
            if timestamp_ms(w['start_utc']) != lower or timestamp_ms(w['end_utc']) != end: raise ValueError('incorrect aggregate bounds')
            if w['coverage'] != 'complete':
                if any(w[k] is not None for k in ('buy_quote','sell_quote','large_net_quote','trade_count','large_cvd_series')): raise ValueError('missing data must be null')
                continue
            if not trades or trades[0]['timestamp_ms'] >= lower or trades[-1]['timestamp_ms'] < end: raise ValueError('false temporal coverage')
            if any(b != a+1 for a,b in zip(ids,ids[1:])): raise ValueError('false ID coverage')
            if any(b['timestamp_ms'] < a['timestamp_ms'] for a,b in zip(trades,trades[1:])): raise ValueError('time goes backwards')
            if e == 'binance' and any(b['first_fill_id'] != a['last_fill_id']+1 for a,b in zip(trades,trades[1:])): raise ValueError('false raw fill continuity')
            selected = [r for r in trades if lower <= r['timestamp_ms'] < end]
            threshold = Decimal(str(d['config']['large_thresholds_quote'][0]))
            large = [r for r in selected if Decimal(r['notional_quote']) >= threshold]
            for label, group in [('', selected), ('large_', large)]:
                buy = sum((Decimal(r['notional_quote']) for r in group if r['taker_side']=='buy'),Decimal(0))
                sell = sum((Decimal(r['notional_quote']) for r in group if r['taker_side']=='sell'),Decimal(0))
                equal(w[label+'buy_quote'],buy,label+'buy'); equal(w[label+'sell_quote'],sell,label+'sell')
                equal(w['large_net_quote' if label else 'cvd_quote'],buy-sell,label+'net')
            equal(w['trade_count'],len(selected),'trade count'); equal(w['large_count'],len(large),'large count')
            cumulative = Decimal(0)
            for point in w['large_cvd_series']:
                upper = timestamp_ms(point['end_utc'])
                total = sum((Decimal(r['notional_quote'])*(1 if r['taker_side']=='buy' else -1) for r in large if lower <= r['timestamp_ms'] < upper),Decimal(0))
                equal(point['cumulative_quote'], total, 'CVD cumulative')
                equal(point['net_quote'],total-cumulative,'CVD increment'); cumulative=total
    for e, book in d['order_books'].items():
        if book['status'] == 'unavailable': continue
        original = raw[e+'_book']
        if book['sequence_continuity_verified'] or book['passive_absorption_inference'] is not None:
            raise ValueError('REST snapshot falsely represented as continuous book evidence')
        totals = []
        for side in ('bids','asks'):
            for a,b in zip(book[side],original[side][:10]):
                equal(a['price'],b[0],'book price'); equal(a['quantity_btc'],b[1],'book size')
                equal(a['notional_quote'],Decimal(b[0])*Decimal(b[1]),'book notional')
            totals.append(sum((Decimal(r[1]) for r in original[side][:10]),Decimal(0)))
        equal(book['imbalance_btc'],(totals[0]-totals[1])/sum(totals),'book imbalance')
    for name,item in d['derivatives'].items():
        if item['status'] != 'available': continue
        original = raw['futures_'+name]
        if name == 'funding':
            if timestamp_ms(item['actual_funding_utc']) != int(original[-1]['fundingTime']): raise ValueError('funding settlement timestamp mismatch')
            equal(item['rate'],original[-1]['fundingRate'],'settled funding')
        elif name == 'oi':
            index = {int(r['timestamp']): r for r in original}
            anchor = timestamp_ms(item['as_of_utc'])
            if anchor != max(index): raise ValueError('OI timestamp mismatch')
            equal(item['open_interest_btc'],index[anchor]['sumOpenInterest'],'OI level')
            for m in (5,15,60):
                w=item['changes'][str(m)+'m']; lower=anchor-m*60000
                if timestamp_ms(w['start_utc']) != lower or timestamp_ms(w['end_utc']) != anchor: raise ValueError('OI range mismatch')
                if lower in index:
                    equal(w['delta_btc'],Decimal(index[anchor]['sumOpenInterest'])-Decimal(index[lower]['sumOpenInterest']),'OI change')
                elif w['delta_btc'] is not None: raise ValueError('unavailable OI change must be null')
        else:
            index={int(r[0]): r for r in original}
            for m in (5,15,60):
                w=item['windows'][str(m)+'m']; lower=end-m*60000
                complete=all(t in index for t in range(lower,end,60000))
                if complete:
                    buy=sum((Decimal(index[t][10]) for t in range(lower,end,60000)),Decimal(0))
                    total=sum((Decimal(index[t][7]) for t in range(lower,end,60000)),Decimal(0))
                    equal(w['buy_quote'],buy,'futures buy');equal(w['sell_quote'],total-buy,'futures sell');equal(w['cvd_quote'],2*buy-total,'futures CVD')
                elif w['cvd_quote'] is not None:raise ValueError('incomplete futures flow must be null')
    checked = refresh_quality(d, now or datetime.now(timezone.utc))
    a = checked['analysis']
    if a['missing_dimensions'] and a['buy_evidence_score_0_100'] is not None: raise ValueError('missing dimensions produced full score')
    if require_usable and not checked['monitoring_eligible']: raise ValueError('stale or incomplete spot data')
    return {'status':'PASS','quality_status':checked['quality_status'], 'monitoring_eligible':checked['monitoring_eligible'],
            'source_archive_verified':True,'predictive_use_eligible':False}


if __name__ == '__main__':
    p = argparse.ArgumentParser(); p.add_argument('--data-dir',type=Path,default=Path('data')); p.add_argument('--require-usable',action='store_true')
    args = p.parse_args(); print(json.dumps(validate(args.data_dir,args.require_usable),indent=2))
