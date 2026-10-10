import copy
from datetime import datetime,timezone,timedelta
from pathlib import Path
import csv,io,json,tempfile,unittest,urllib.error
from unittest.mock import patch
import collector
from quality import refresh_quality

class QualityTests(unittest.TestCase):
    def fresh_data(self):
        return {'generated_utc':'2026-10-10T13:02:00Z','latest_closed_5m_end_utc':'2026-10-10T13:00:00Z','errors':{},'last_intervals':[{'coverage':'complete'},{'coverage':'complete'}],'markets':{exchange:{frame:{'coverage':'complete','missing_intervals':[]} for frame in ('15m','60m')} for exchange in ('binance','coinbase')}}

    def test_stale_is_recomputed_when_static_json_stops_updating(self):
        data=self.fresh_data()
        now=datetime(2026,10,10,13,18,tzinfo=timezone.utc)
        result=refresh_quality(data,now)
        self.assertEqual(result['data_age_seconds'],960)
        self.assertEqual(result['quality_status'],'stale')
        self.assertFalse(result['monitoring_eligible'])
        self.assertNotIn('quality_status',data)

    def test_failed_source_blocks_monitoring_even_with_cached_history(self):
        data=self.fresh_data();data['errors']={'coinbase':'timeout'}
        result=refresh_quality(data,datetime(2026,10,10,13,3,tzinfo=timezone.utc))
        self.assertEqual(result['quality_status'],'incomplete')
        self.assertFalse(result['monitoring_eligible'])

    def test_old_market_data_is_stale_even_if_json_was_just_regenerated(self):
        data=self.fresh_data();data['latest_closed_5m_end_utc']='2026-10-10T12:40:00Z'
        self.assertEqual(refresh_quality(data,datetime(2026,10,10,13,3,tzinfo=timezone.utc))['quality_status'],'stale')

    def test_coinbase_old_snapshot_cannot_claim_complete_empty_latest_interval(self):
        trades=[{'trade_id':2,'time':collector.utc(100000),'side':'sell','price':'10','size':'2'}, {'trade_id':1,'time':collector.utc(-1),'side':'buy','price':'10','size':'2'}]
        with patch('collector.get',return_value=(trades,{})):
            row=collector.coinbase(0,300000)[0]
        self.assertEqual(row['coverage'],'incomplete')
        self.assertIsNone(row['cvd_usd'])

    def test_coinbase_trade_id_gap_is_fail_closed(self):
        trades=[{'trade_id':3,'time':collector.utc(300000),'side':'sell','price':'10','size':'2'}, {'trade_id':1,'time':collector.utc(-1),'side':'buy','price':'10','size':'2'}]
        with patch('collector.get',return_value=(trades,{})):
            row=collector.coinbase(0,300000)[0]
        self.assertEqual(row['coverage'],'incomplete')
        self.assertIsNone(row['taker_ratio'])

    def test_coinbase_two_pages_cover_window_and_deduplicate_boundary(self):
        def trade(ident,ms,side):return {'trade_id':ident,'time':collector.utc(ms),'side':side,'price':'10','size':'2'}
        first=[trade(4,300000,'sell'),trade(3,100000,'sell')]
        second=[first[-1],trade(2,0,'buy'),trade(1,-1,'buy')]
        with patch('collector.get',side_effect=[(first,{'cb-after':'3'}),(second,{})]),patch('collector.time.sleep'):
            row=collector.coinbase(0,300000)[0]
        self.assertEqual(row['coverage'],'complete')
        self.assertEqual(row['trades'],2)
        self.assertEqual(row['cvd_usd'],0)
        self.assertEqual(collector.FETCH_STATS['coinbase']['duplicate_trades'],1)

    def test_binance_duplicate_candle_is_rejected(self):
        candle=[0,0,0,0,0,0,59999,'100',4,0,'60',0]
        with patch('collector.get',return_value=([candle,candle],{})),self.assertRaises(ValueError):
            collector.binance(0,300000)

    def test_incomplete_interval_has_no_numeric_directional_metrics(self):
        row=collector.entry('coinbase',0,300000,100,0,2,'incomplete','missing cursor')
        for field in ('buy_usd','sell_usd','cvd_usd','taker_ratio'):self.assertIsNone(row[field])

    def test_atomic_failure_does_not_destroy_valid_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'history.csv';path.write_text('previous valid data\n')
            def failed(handle):handle.write('partial');raise OSError('disk failure')
            with self.assertRaises(OSError):collector.atomic_write(path,failed)
            self.assertEqual(path.read_text(),'previous valid data\n')

    def test_successful_history_is_not_changed_by_refetch(self):
        with tempfile.TemporaryDirectory() as directory,patch('collector.DATA',Path(directory)):
            collector.save([collector.entry('binance',0,300000,100,50,1,'complete')])
            collector.save([collector.entry('binance',0,300000,999,50,1,'complete')])
            with (Path(directory)/'history.csv').open(newline='') as handle:rows=list(csv.DictReader(handle))
        self.assertEqual(float(rows[0]['buy_usd']),100)

    def test_retry_after_is_observed_and_retry_is_logged(self):
        class Response(io.StringIO):
            headers={}
        error=urllib.error.HTTPError('https://example.test',429,'rate limited',{'Retry-After':'3'},None)
        with patch('collector.urllib.request.urlopen',side_effect=[error,Response('[]')]),patch('collector.time.sleep') as sleep,patch('collector.log') as log:
            data,_=collector.get('https://example.test')
        self.assertEqual(data,[]);sleep.assert_called_once_with(3)
        self.assertEqual(log.call_args.kwargs['status'],429)

    def test_expired_network_deadline_stops_requests(self):
        with patch('collector.FETCH_DEADLINE',0),patch('collector.time.monotonic',return_value=1),patch('collector.time.sleep'),self.assertRaises(TimeoutError):
            collector.remaining_seconds()

    def test_missing_intervals_lists_exact_missing_start(self):
        rows=[collector.entry('binance',t,t+300000,100,50,1,'complete') for t in (0,600000)]
        window=collector.windows_for(rows,900000)['binance']['15m']
        self.assertEqual(window['missing_intervals'],[collector.utc(300000)])
        self.assertEqual(window['intervals'],2)
        self.assertIsNone(window['cvd_usd'])
