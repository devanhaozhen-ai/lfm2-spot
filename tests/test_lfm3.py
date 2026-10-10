import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
import collector
import lfm3
from validate_lfm3 import validate, reference_ema, reference_rsi
from work_collect import preserve_extended_history


class LFM3Tests(unittest.TestCase):
    def test_indicators_known_values_and_independent_formula(self):
        self.assertEqual(lfm3.ema(list(range(1,9)),3), 7)
        classic = [44.34,44.09,44.15,43.61,44.33,44.83,45.10,45.42,45.84,46.08,45.89,46.03,45.61,46.28,46.28]
        self.assertAlmostEqual(lfm3.rsi(classic),70.4641350211,places=8)
        self.assertAlmostEqual(lfm3.rsi(classic),reference_rsi(classic),places=10)
        values = [100+i*.1+(i%7) for i in range(200)]
        self.assertAlmostEqual(lfm3.ema(values,60),reference_ema(values,60),places=9)
        self.assertEqual(lfm3.rsi([100]*100),50)
        self.assertEqual(lfm3.rsi(list(range(100))),100)
        self.assertEqual(lfm3.rsi(list(range(100,0,-1))),0)

    def test_coinbase_inclusive_end_is_excluded_and_fields_are_correct(self):
        raw = [[600,9,12,10,11,3], [300,8,11,9,10,2], [0,7,10,8,9,1]]
        with patch('collector.get', return_value=(raw, {})), patch('lfm3.time.sleep'):
            rows = lfm3.fetch_prices('coinbase','5m',0,600000)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[-1]['start_utc'],collector.utc(300000))
        self.assertEqual((rows[-1]['open'],rows[-1]['low'],rows[-1]['high'],rows[-1]['close']), (9,8,11,10))
        self.assertIsNone(rows[-1]['taker_buy_quote'])

    def test_complete_observations_cannot_be_overwritten_and_unavailable_can_upgrade(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'history.csv'
            a=collector.entry('coinbase',0,300000,10,20,2,'complete')
            b=collector.entry('coinbase',300000,600000,0,0,0,'unavailable','not retrieved')
            lfm3.persist(p,[a,b],collector.FIELDS,lfm3.cvd_key)
            changed=dict(a,buy_usd=999)
            good=collector.entry('coinbase',300000,600000,30,20,3,'complete')
            rows=lfm3.persist(p,[changed,good],collector.FIELDS,lfm3.cvd_key)
            self.assertEqual(float(rows[0]['buy_usd']),10)
            self.assertEqual(rows[1]['coverage'],'complete')
            before=p.read_bytes()
            lfm3.persist(p,[],collector.FIELDS,lfm3.cvd_key)
            self.assertEqual(before,p.read_bytes())

    def test_duplicate_or_misaligned_candles_are_rejected(self):
        raw=[ [0,'10','12','9','11','1',299999,'11',2,'.5','5','0'],
              [0,'10','12','9','11','2',299999,'22',2,'.5','5','0'] ]
        with patch('collector.get', return_value=(raw,{})):
            with self.assertRaisesRegex(ValueError,'conflicting'): lfm3.fetch_prices('binance','5m',0,300000)
        raw[0][6]=299998
        with patch('collector.get', return_value=(raw[:1],{})):
            with self.assertRaisesRegex(ValueError,'boundary'): lfm3.fetch_prices('binance','5m',0,300000)

    def prices(self, exchange, frame, start, end):
        symbol,currency=lfm3.UNITS[exchange]
        rows=[]
        for i,t in enumerate(range(start,end,lfm3.FRAMES[frame])):
            close=100+i*.03+(i%9)*.01
            rows.append(dict(exchange=exchange,symbol=symbol,quote_currency=currency,timeframe=frame,
                start_utc=collector.utc(t),end_utc=collector.utc(t+lfm3.FRAMES[frame]),
                open=close-.01,high=close+.10,low=close-.10,close=close,volume_btc=2+(i%5),
                quote_volume='100' if exchange=='binance' else None,taker_buy_quote='40' if exchange=='binance' else None,
                trades=3 if exchange=='binance' else None,coverage='complete',source=lfm3.SOURCES[exchange]))
        return rows

    def fixture(self,d):
        clock=datetime.now(timezone.utc)
        end=int(clock.timestamp()*1000)//300000*300000
        baseline=dict(generated_utc=collector.utc(int(clock.timestamp()*1000)),latest_closed_5m_end_utc=collector.utc(end),
            collection_mode='manual_workflow_dispatch',request_id='test',collection_run_id='123',errors={},
            markets={e:{f:dict(coverage='complete',intervals=n,expected=n,missing_intervals=[],buy_usd=40*n,sell_usd=60*n,cvd_usd=-20*n,taker_ratio=.66667) for f,n in (('15m',3),('60m',12))} for e in ('binance','coinbase')},
            last_intervals=[dict(coverage='complete'),dict(coverage='complete')])
        (d/'latest.json').write_text(json.dumps(baseline))
        rows=[collector.entry(e,t,t+300000,40,60,3,'complete','measured') for e in ('binance','coinbase') for t in range(end-3600000,end,300000)]
        with (d/'history.csv').open('w',newline='') as h:
            w=csv.DictWriter(h,fieldnames=collector.FIELDS);w.writeheader();w.writerows(rows)
        return clock,end

    def test_full_seven_day_price_pipeline_and_truthful_unavailable_cvd(self):
        with tempfile.TemporaryDirectory() as root:
            d=Path(root);_,end=self.fixture(d)
            before=(d/'history.csv').read_bytes()
            with patch('lfm3.fetch_prices',side_effect=self.prices): output=lfm3.collect(d)
            self.assertEqual(before,(d/'history.csv').read_bytes())
            self.assertEqual(output['markets']['binance']['5m']['intervals'],2016)
            self.assertEqual(output['markets']['coinbase']['1h']['intervals'],168)
            self.assertEqual(output['markets']['binance']['historical_cvd']['available'],2016)
            self.assertEqual(output['markets']['coinbase']['historical_cvd']['available'],12)
            self.assertIsNone(output['markets']['coinbase']['historical_cvd']['net_cvd_quote'])
            self.assertEqual(output['historical_cvd_quality'],'partial')
            self.assertTrue(output['monitoring_eligible'])
            self.assertEqual(validate(d)['status'],'PASS')
            # Static success ages out on read, even if history is still complete.
            checked=lfm3.refresh_lfm3_quality(output,datetime.fromtimestamp(end/1000,timezone.utc)+timedelta(minutes=16))
            self.assertFalse(checked['monitoring_eligible'])
            self.assertEqual(checked['quality_status'],'stale')
            tampered=json.loads((d/'lfm3_latest.json').read_text())
            tampered['markets']['coinbase']['5m']['indicators']['rsi14']=1
            (d/'lfm3_latest.json').write_text(json.dumps(tampered))
            with self.assertRaisesRegex(ValueError,'RSI14'): validate(d)

    def test_partial_exchange_failure_retains_other_valid_data(self):
        with tempfile.TemporaryDirectory() as root:
            d=Path(root);self.fixture(d)
            def fetch(e,f,s,t):
                if e=='coinbase': raise TimeoutError('API unavailable')
                return self.prices(e,f,s,t)
            with patch('lfm3.fetch_prices',side_effect=fetch): output=lfm3.collect(d)
            self.assertEqual(output['markets']['binance']['5m']['coverage'],'complete')
            self.assertEqual(output['markets']['coinbase']['5m']['coverage'],'incomplete')
            self.assertIn('coinbase',output['errors'])
            self.assertFalse(output['monitoring_eligible'])
            self.assertEqual(validate(d,require_usable=False)['status'],'PASS')

    def test_extended_history_preservation_detects_changed_or_duplicate_rows(self):
        original=b'exchange,timeframe,start_utc,coverage,close\nbinance,5m,2026-10-10T14:00:00Z,complete,100\n'
        with self.assertRaises(ValueError): preserve_extended_history(original,original.replace(b',100',b',200'),True)
        with self.assertRaises(ValueError): preserve_extended_history(original,original+original.splitlines(keepends=True)[1],True)


if __name__=='__main__':
    unittest.main()
