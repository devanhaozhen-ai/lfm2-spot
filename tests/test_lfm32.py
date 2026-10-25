import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import lfm32
import lfm32_sources as sources
from lfm32_orderbook import BinanceBook, SequenceGap
from validate_lfm32 import validate
from collector import utc

START, END = 3600000, 7200000


def trade(i, t, maker=False, size='1', exchange='binance'):
    if exchange == 'binance':
        return {'a':i,'p':'100000','q':size,'f':i,'l':i,'T':t,'m':maker}
    return {'trade_id':i,'price':'100000','size':size,'time':utc(t),'side':'buy' if maker else 'sell'}


def stream(exchange='binance'):
    rows = [sources.normalize(exchange, trade(i,t, i%2==0,exchange=exchange)) for i,t in enumerate([START-1,START,START+300000,END-300000,END],1)]
    return {'trades':rows,'id_gaps':0,'fill_id_gaps':False,'reversed_time':False,'coverage':'complete','reason':'temporal_brackets_reached'}


class FakeClient:
    def __init__(self,pages): self.pages=iter(pages); self.calls=[]
    def get(self,url,params=None): self.calls.append((url,params)); return next(self.pages)


class LFM32Tests(unittest.TestCase):
    def setUp(self): self.config=lfm32.load_config()

    def test_maker_direction_both_exchanges(self):
        for e in ('binance','coinbase'):
            for maker in (True,False):
                t=sources.normalize(e,trade(1,START,maker,exchange=e))
                self.assertEqual(t['taker_side'],'sell' if maker else 'buy')
        bad=trade(1,START);bad['m']='false'
        with self.assertRaises(ValueError): sources.normalize('binance',bad)

    def test_exact_notional_and_threshold_tiers(self):
        r=sources.normalize('binance',{'a':1,'p':'60000.125','q':'2.5','f':1,'l':1,'T':START,'m':False})
        self.assertEqual(Decimal(r['notional_quote']),Decimal('150000.3125'))
        s=stream();s['trades'][1]=sources.normalize('binance',trade(2,START,False,size='5'))
        a=lfm32.aggregate(s,START,END,self.config)
        self.assertEqual(a['tiers'][1]['buy_quote'],500000)
        self.assertEqual(a['tiers'][0]['buy_count'],1)
        self.assertEqual(a['large_net_quote'],500000)
        self.assertEqual(a['large_cvd_series'][-1]['cumulative_quote'],a['large_net_quote'])

    @patch('lfm32_sources.time.sleep')
    def test_binance_forward_pagination_deduplicates_aggregate_groups(self,_):
        c=FakeClient([([trade(1,START-1),trade(2,START)],{}),([trade(2,START),trade(3,END)],{})])
        s=sources.fetch_trades('binance',START,END,c,2)
        self.assertEqual(s['coverage'],'complete');self.assertEqual(len(s['trades']),3)
        self.assertEqual(c.calls[1][1]['fromId'],3)
        self.assertEqual(lfm32.aggregate(s,START,END,self.config)['trade_count'],1)
        self.assertIsNone(s['trades'][1]['unique_taker_orders'])

    @patch('lfm32_sources.time.sleep')
    def test_coinbase_backward_cursor_and_duplicate_boundary(self,_):
        c=FakeClient([([trade(3,END,exchange='coinbase'),trade(2,START,exchange='coinbase')],{'cb-after':'2'}),
                      ([trade(2,START,exchange='coinbase'),trade(1,START-1,exchange='coinbase')],{'cb-after':'1'})])
        s=sources.fetch_trades('coinbase',START,END,c,2)
        self.assertEqual(s['coverage'],'complete');self.assertEqual(len(s['trades']),3)
        self.assertEqual(c.calls[1][1]['after'],'2')

    @patch('lfm32_sources.time.sleep')
    def test_id_gap_fill_gap_page_limit_and_missing_cursor_do_not_become_zero(self,_):
        for e,pages in [('binance',[([trade(1,START-1),trade(3,END)],{})]),
                        ('coinbase',[([trade(3,END,exchange='coinbase'),trade(2,START,exchange='coinbase')],{})]),
                        ('binance',[([trade(1,START-1),trade(2,START)],{})])]:
            s=sources.fetch_trades(e,START,END,FakeClient(pages),1)
            self.assertNotEqual(s['coverage'],'complete')
            self.assertIsNone(lfm32.aggregate(s,START,END,self.config)['large_net_quote'])
        r=trade(2,END);r['f']=r['l']=4
        s=sources.fetch_trades('binance',START,END,FakeClient([([trade(1,START-1),r],{})]),1)
        self.assertTrue(s['fill_id_gaps']);self.assertIsNone(lfm32.aggregate(s,START,END,self.config)['trade_count'])

    def test_conflicting_duplicate_rejected(self):
        with patch('lfm32_sources.time.sleep'):
            c=FakeClient([([trade(1,START-1),trade(2,START)],{}),([trade(2,START,size='2'),trade(3,END)],{})])
            with self.assertRaises(ValueError): sources.fetch_trades('binance',START,END,c,2)

    def test_currency_separation_dynamic_quantile_and_window_anchor(self):
        self.config['dynamic_min_samples']=1
        for e,unit in [('binance','USDT'),('coinbase','USD')]:
            m=lfm32.build_market(e,stream(e),START,END,self.config)
            self.assertEqual(m['quote_currency'],unit)
            self.assertEqual(m['dynamic']['threshold_quote'],100000)
            self.assertEqual(m['windows']['60m']['large_cvd_anchor_utc'],utc(START))

    def test_oi_changes_exact_timestamps_missing_is_null(self):
        raw=[{'symbol':'BTCUSDT','timestamp':t,'sumOpenInterest':str(t//300000),'sumOpenInterestValue':'2000000'} for t in range(START,END+1,300000)]
        r=sources.fetch_oi(FakeClient([(raw,{})]),END)
        self.assertEqual(r['as_of_utc'],utc(END))
        self.assertEqual(r['changes']['5m']['delta_btc'],1)
        self.assertEqual(r['changes']['15m']['delta_btc'],3)
        self.assertEqual(r['changes']['60m']['delta_btc'],12)
        raw.pop(0)
        r=sources.fetch_oi(FakeClient([(raw,{})]),END)
        self.assertIsNone(r['changes']['60m']['delta_btc'])

    def test_funding_settlement_is_not_retrieval_time(self):
        raw=[{'symbol':'BTCUSDT','fundingTime':START,'fundingRate':'-0.00002'}]
        r=sources.fetch_funding(FakeClient([(raw,{})]))
        self.assertEqual(r['actual_funding_utc'],utc(START))
        self.assertEqual(r['rate_kind'],'last_settled')

    def test_futures_flow_partial_candles_and_units(self):
        raw=[[t,'1','1','1','100000','2',t+59999,'200000',1,'1','120000'] for t in range(START,END,60000)]
        r=sources.fetch_futures_flow(FakeClient([(raw,{})]),START,END)
        self.assertEqual(r['windows']['5m']['cvd_quote'],200000)
        self.assertEqual(r['quote_currency'],'USDT')
        raw.pop()
        r=sources.fetch_futures_flow(FakeClient([(raw,{})]),START,END)
        self.assertIsNone(r['windows']['60m']['cvd_quote'])

    def test_order_book_buffer_replay_stale_and_gap_invalidation(self):
        b=BinanceBook({'lastUpdateId':10,'bids':[['99','2']],'asks':[['101','3']]})
        self.assertFalse(b.valid)
        self.assertFalse(b.apply({'U':8,'u':10,'b':[],'a':[]}))
        self.assertTrue(b.apply({'U':9,'u':12,'b':[['99','0'],['98','5']],'a':[]}))
        self.assertNotIn(Decimal('99'),b.bids);self.assertTrue(b.valid)
        self.assertTrue(b.apply({'U':13,'u':14,'b':[],'a':[['101','4']]}))
        with self.assertRaises(SequenceGap): b.apply({'U':16,'u':17,'b':[],'a':[]})
        self.assertFalse(b.valid)
        with self.assertRaises(SequenceGap): b.apply({'U':15,'u':16,'b':[],'a':[]})

    def test_snapshot_is_never_treated_as_continuous_book_or_absorption(self):
        raw={'lastUpdateId':123,'bids':[[str(100-i),str(i+1)] for i in range(10)],'asks':[[str(101+i),'1'] for i in range(10)]}
        b=sources.book_snapshot('binance',FakeClient([(raw,{})]),Decimal(100))
        self.assertEqual(len(b['bids']),10)
        self.assertFalse(b['sequence_continuity_verified']);self.assertIsNone(b['cancellations'])
        self.assertIsNone(b['iceberg_inference'])

    def observation(self):
        d={'schema_version':'3.2','generated_utc':utc(END+1000),'window_end_utc':utc(END),
           'config':self.config,'markets':{e:lfm32.build_market(e,stream(e),START,END,self.config) for e in ('binance','coinbase')},'derivatives':{},'order_books':{}}
        return d

    def test_read_time_900_second_boundary_disables_score_and_signals(self):
        now=datetime.fromtimestamp(END/1000,tz=timezone.utc)
        d=self.observation()
        a=lfm32.refresh_quality(d,now+timedelta(seconds=900))
        self.assertTrue(a['monitoring_eligible'])
        b=lfm32.refresh_quality(a,now+timedelta(seconds=901))
        self.assertFalse(b['monitoring_eligible']);self.assertIsNone(b['analysis']['observed_evidence_points'])
        self.assertEqual(b['analysis']['signals'],['数据不足，无法判断'])

    def test_missing_dimensions_not_zero_or_renormalized(self):
        d=lfm32.refresh_quality(self.observation(),datetime.fromtimestamp(END/1000,tz=timezone.utc))
        a=d['analysis'];self.assertIsNone(a['buy_evidence_score_0_100'])
        self.assertIn('book_absorption',a['missing_dimensions']);self.assertEqual(a['available_weight'],55)
        self.assertFalse(a['predictive_use_eligible'])

    def test_single_venue_failure_does_not_pollute_other_venue(self):
        d=self.observation();d['markets']['coinbase']=lfm32.build_market('coinbase',{'trades':[],'error':'HTTP403'},START,END,self.config)
        d=lfm32.refresh_quality(d,datetime.fromtimestamp(END/1000,tz=timezone.utc))
        self.assertTrue(d['markets']['binance']['monitoring_eligible'])
        self.assertFalse(d['markets']['coinbase']['monitoring_eligible']);self.assertFalse(d['monitoring_eligible'])
        self.assertIsNone(d['analysis']['buy_evidence_score_0_100'])

    def test_future_time_and_stale_oi_cannot_confirm_score(self):
        d=self.observation();d['derivatives']['oi']={'status':'available','as_of_utc':utc(END-1000000)}
        d=lfm32.refresh_quality(d,datetime.fromtimestamp(END/1000,tz=timezone.utc))
        self.assertFalse(d['derivatives']['oi']['fresh_at_read'])
        d['generated_utc']=utc(END+120000)
        self.assertFalse(lfm32.refresh_quality(d,datetime.fromtimestamp(END/1000,tz=timezone.utc))['monitoring_eligible'])

    def test_immutable_complete_history_and_unavailable_can_be_repaired(self):
        row={'exchange':'binance','start_utc':utc(START),'config_hash':'x','coverage':'complete','buy_quote':10}
        with tempfile.TemporaryDirectory() as tmp:
            p=Path(tmp)/'history.jsonl'
            lfm32.persist_history(p,[row]);lfm32.persist_history(p,[{**row,'buy_quote':999}])
            self.assertEqual(json.loads(p.read_text())['buy_quote'],10)
            other={**row,'exchange':'coinbase','coverage':'unavailable','buy_quote':None}
            lfm32.persist_history(p,[other]);lfm32.persist_history(p,[{**other,'coverage':'complete','buy_quote':20}])
            self.assertEqual(len(p.read_text().splitlines()),2)

    def test_schedule_manual_metadata_real_collect_and_independent_archive_validation(self):
        def fetch(e,start,end,*_):
            s=stream(e)
            # Shift the fixed fixture to the collector's actual closed window.
            shift=start-START
            for r in s['trades']:
                r['timestamp_ms']+=shift;r['time_utc']=utc(r['timestamp_ms'])
                if e=='binance':r['source_record']['T']+=shift
                else:r['source_record']['time']=r['time_utc']
            return s
        for mode in ('scheduled','manual_workflow_dispatch'):
            with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ,{'LFM2_COLLECTION_MODE':mode,'LFM2_REQUEST_ID':'test'}), \
                 patch('lfm32.fetch_trades',side_effect=fetch),patch('lfm32.book_snapshot',side_effect=OSError('test unavailable')), \
                 patch('lfm32.fetch_oi',side_effect=OSError('451')),patch('lfm32.fetch_funding',side_effect=OSError('451')),patch('lfm32.fetch_futures_flow',side_effect=OSError('451')):
                legacy=Path(tmp)/'latest.json';legacy.write_text('legacy retained')
                d=lfm32.collect(tmp,self.config)
                self.assertEqual(d['collection_mode'],mode);self.assertEqual(d['request_id'],'test')
                self.assertEqual(legacy.read_text(),'legacy retained')
                self.assertEqual(validate(tmp)['status'],'PASS')
                bad=json.loads((Path(tmp)/'lfm32_latest.json').read_text());bad['markets']['binance']['windows']['60m']['large_net_quote']=123
                (Path(tmp)/'lfm32_latest.json').write_text(json.dumps(bad))
                with self.assertRaises(ValueError):validate(tmp)

    def test_manual_dispatcher_matches_current_workflow_title(self):
        import time
        import work_collect
        run={'display_title':'BTC collection new-request','head_branch':'main','id':123}
        with patch('work_collect.api',return_value={'workflow_runs':[run]}):
            self.assertEqual(work_collect.find_run('new-request',time.monotonic()+2)['id'],123)

    def test_book_malformed_event_invalidates_previous_valid_state(self):
        b=BinanceBook({'lastUpdateId':1,'bids':[['99','2']],'asks':[['101','3']]})
        b.apply({'U':2,'u':2,'b':[],'a':[]})
        with self.assertRaises(SequenceGap):b.apply({'U':3,'u':3,'b':[['99','-1']],'a':[]})
        self.assertFalse(b.valid)

    def test_existing_schedule_dispatch_and_quality_checks_remain(self):
        root=Path(__file__).resolve().parents[1]
        workflow=(root/'.github/workflows/collect.yml').read_text()
        self.assertIn("cron: '2-59/5 * * * *'",workflow);self.assertIn('workflow_dispatch:',workflow)
        self.assertIn('python validate_lfm3.py --allow-partial',workflow);self.assertIn('run: python validate_lfm3.py\n',workflow)
        self.assertIn("'scheduled' || 'manual_workflow_dispatch'",workflow)


if __name__=='__main__': unittest.main()
