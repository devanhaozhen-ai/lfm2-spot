import csv,json,math,time,urllib.request,urllib.parse
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).parent; DATA=ROOT/'data';DATA.mkdir(exist_ok=True)
HEAD={'User-Agent':'LFM2-Spot-Monitor/1.0','Accept':'application/json'}
def get(url,params=None):
    if params:url+='?'+urllib.parse.urlencode(params)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(urllib.request.Request(url,headers=HEAD),timeout=20) as r:return json.load(r),{k.lower():v for k,v in r.headers.items()}
        except Exception:
            if attempt==3:raise
            time.sleep(2**attempt)
def utc(ms):return datetime.fromtimestamp(ms/1000,timezone.utc).isoformat().replace('+00:00','Z')
def timestamp_ms(value):return int(datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()*1000)
def save(rows):
    path=DATA/'history.csv'; fields=['exchange','start_utc','end_utc','buy_usd','sell_usd','cvd_usd','taker_ratio','trades','coverage','reason']
    old={}
    if path.exists():
        with path.open(newline='') as f:
            for r in csv.DictReader(f):old[(r['exchange'],r['start_utc'])]=r
    for r in rows:
        key=(r['exchange'],r['start_utc'])
        if key in old and old[key]['coverage']=='complete' and r['coverage']!='complete':continue
        old[key]=r
    values=sorted(old.values(),key=lambda x:(x['start_utc'],x['exchange']))
    with path.open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(values)
def entry(exchange,start,end,buy,sell,n,coverage,reason=''):
    return dict(exchange=exchange,start_utc=utc(start),end_utc=utc(end),buy_usd=round(buy,2),sell_usd=round(sell,2),cvd_usd=round(buy-sell,2),taker_ratio=round(buy/sell,5) if sell else None,trades=n,coverage=coverage,reason=reason)
def binance(start,end):
    # Fetch the last hour once, then verify each closed five-minute interval.
    arr,_=get('https://data-api.binance.vision/api/v3/klines',{'symbol':'BTCUSDT','interval':'1m','startTime':start,'endTime':end-1,'limit':1000})
    candles={int(x[0]):x for x in arr};rows=[]
    for slot in range(start,end,300000):
        expected=list(range(slot,slot+300000,60000))
        if any(t not in candles or int(candles[t][6])!=t+59999 for t in expected):
            rows.append(entry('binance',slot,slot+300000,0,0,0,'unavailable','missing or unclosed 1-minute candles'))
            continue
        subset=[candles[t] for t in expected]
        for candle in subset:
            total=float(candle[7]);buy=float(candle[10])
            if not math.isfinite(total) or not math.isfinite(buy) or not 0<=buy<=total:
                raise ValueError('invalid candle taker volume')
        buy=sum(float(x[10]) for x in subset);total=sum(float(x[7]) for x in subset)
        rows.append(entry('binance',slot,slot+300000,buy,total-buy,sum(int(x[8]) for x in subset),'complete','1m closed candle taker quote volumes'))
    return rows
def coinbase(start,end):
    # One paginated scan covers the hour; only fully traversed intervals are complete.
    url='https://api.exchange.coinbase.com/products/BTC-USD/trades';cursor=None;seen=set();cursors=set();oldest=None
    buckets={slot:[0.,0.,0] for slot in range(start,end,300000)}
    for page in range(80):
        params={'limit':1000}
        if cursor is not None:params['after']=cursor
        arr,headers=get(url,params)
        if not arr:break
        for t in arr:
            ident=t['trade_id']
            if ident in seen:continue
            seen.add(ident)
            ms=int(datetime.fromisoformat(t['time'].replace('Z','+00:00')).timestamp()*1000)
            oldest=ms if oldest is None else min(oldest,ms)
            if ms<start:continue
            if ms>=end:continue
            value=float(t['price'])*float(t['size'])
            if not math.isfinite(value) or value<0 or t['side'] not in ('sell','buy'):
                raise ValueError('invalid trade side or notional')
            # Coinbase side is MAKER side; sell maker means taker BUY.
            bucket=buckets[(ms//300000)*300000]
            bucket[0 if t['side']=='sell' else 1]+=value
            bucket[2]+=1
        if oldest is not None and oldest<start:break
        cursor=next((v for k,v in headers.items() if k.lower()=='cb-after'),None)
        if not cursor or cursor in cursors:break
        cursors.add(cursor)
        time.sleep(.18)
    rows=[]
    for slot,(buy,sell,n) in buckets.items():
        complete=oldest is not None and oldest<slot
        rows.append(entry('coinbase',slot,slot+300000,buy,sell,n,'complete' if complete else 'incomplete', '' if complete else 'pagination cap or missing cursor; do not use as complete CVD'))
    return rows
def windows_for(history,end):
    windows={}
    for name in ('binance','coinbase'):
        windows[name]={}
        for minutes in (15,60):
            window_start=end-minutes*60000
            # Closed five-minute history starts belong to [window_start, end).
            subset=[r for r in history if r['exchange']==name and r['coverage']=='complete' and window_start<=timestamp_ms(r['start_utc'])<end]
            expected=set(range(window_start,end,300000))
            ok={timestamp_ms(r['start_utc']) for r in subset}==expected and len(subset)==minutes//5 and all(timestamp_ms(r['end_utc'])==timestamp_ms(r['start_utc'])+300000 for r in subset)
            buy=sum(float(r['buy_usd']) for r in subset) if ok else None
            sell=sum(float(r['sell_usd']) for r in subset) if ok else None
            windows[name][str(minutes)+'m']={'coverage':'complete' if ok else 'incomplete','intervals':len(subset),'expected':minutes//5,'buy_usd':round(buy,2) if ok else None,'sell_usd':round(sell,2) if ok else None,'cvd_usd':round(buy-sell,2) if ok else None,'taker_ratio':round(buy/sell,5) if ok and sell else None}
    return windows
def main():
    now=int(time.time()*1000);end=(now//300000)*300000;start=end-3600000
    rows=[];all_rows=[];errors={}
    for name,fn in [('binance',binance),('coinbase',coinbase)]:
        try:
            fetched=fn(start,end)
            if len(fetched)!=12:raise ValueError('missing five-minute interval results')
        except Exception as e:
            fetched=[entry(name,t,t+300000,0,0,0,'unavailable',str(e)) for t in range(start,end,300000)]
        failures=[r['reason'] for r in fetched if r['coverage']!='complete']
        if failures:errors[name]='; '.join(sorted(set(failures)))
        all_rows.extend(fetched);rows.append(fetched[-1])
    save(all_rows)
    history=[]
    with (DATA/'history.csv').open(newline='') as f:history=list(csv.DictReader(f))
    windows=windows_for(history,end)
    out={'schema_version':'1.0','generated_utc':utc(now),'latest_closed_5m_end_utc':utc(end),'markets':windows,'last_intervals':rows,'warnings':['Coinbase side is maker side and must be inverted for taker direction.','Incomplete windows must not be used for directional signals.','Binance BTCUSDT quote is USDT, approximated as USD for comparison.'],'errors':errors}
    (DATA/'latest.json').write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'end':utc(end),'coverage':{k:v['15m']['coverage'] for k,v in windows.items()},'errors':errors}))
if __name__=='__main__':main()
