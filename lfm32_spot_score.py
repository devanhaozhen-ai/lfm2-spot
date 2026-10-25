"""Separate, per-venue experimental spot evidence rubric. No pooled whale score."""


def score_spot(data):
    results = {}
    for exchange,m in data['markets'].items():
        dims = {k:{'weight':w,'value':None,'reason':'missing or stale spot inputs'} for k,w in
                [('all_taker_flow',50),('relative_large_records',25),('price_volume',25)]}
        if m.get('monitoring_eligible'):
            w=m['windows']['15m']; total=w['buy_quote']+w['sell_quote']
            if total:
                dims['all_taker_flow'].update(value=w['buy_quote']/total,reason='15m taker buy / (buy + sell), native quote amounts within this venue')
            dynamic=m['dynamic']; dw=(dynamic.get('windows') or {}).get('15m',{})
            if dw.get('coverage')=='complete':
                large=dw['large_buy_quote']+dw['large_sell_quote']
                if large:
                    dims['relative_large_records'].update(value=dw['large_buy_quote']/large,
                        reason='15m buy share of feed records above this venue\'s same-window 60m empirical quantile; record granularity differs across venues')
                else: dims['relative_large_records']['reason']='verified window has no qualifying dynamic large records; ratio undefined, not missing amounts filled with zero'
            w5=m['windows']['5m']; prior=(w.get('volume_btc',0)-w5.get('volume_btc',0))/2
            p=w.get('price_change_pct')
            if p is not None and prior>0:
                value=1 if p>0 and w5['volume_btc']>prior else 0 if p<0 and w5['volume_btc']>prior else .5
                dims['price_volume'].update(value=value,reason='15m price direction with last5m BTC volume vs previous two 5m bars; inconclusive=0.5')
        active=[k for k,d in dims.items() if d['value'] is not None]
        missing=[k for k in dims if k not in active]
        points=sum(dims[k]['value']*dims[k]['weight'] for k in active)
        results[exchange]={'score_0_100':round(points,3) if not missing and m.get('monitoring_eligible') else None,
                           'observed_points':round(points,3) if active else None,
                           'available_weight':sum(dims[k]['weight'] for k in active),
                           'dimensions':dims,'effective_dimensions':active,'missing_dimensions':missing,
                           'quote_currency':m['quote_currency'],'trade_kind':m.get('trade_kind'),
                           'confidence':'limited_unvalidated' if not missing else 'insufficient'}
    return {'name':'experimental_per_venue_spot_buy_evidence', 'version':'1', 'per_venue':results,
            'cross_exchange_composite_0_100':None, 'cross_exchange_score_comparison_eligible':False,
            'scope':'spot flow only; no OI, funding, passive absorption or liquidation evidence',
            'predictive_use_eligible':False, 'institutional_probability':False,
            'calibration':'50/25/25 explicit spot-only rubric; not a rescaling of the full score. No backtest or out-of-sample validation. Dynamic record sizes and count granularity differ across exchanges.'}
