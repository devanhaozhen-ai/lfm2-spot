import csv
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import collector


class CollectorTests(unittest.TestCase):
    def test_coinbase_maker_side_dedup_and_boundaries(self):
        def trade(ident, ms, side, price='10', size='2'):
            return {'trade_id': ident, 'time': collector.utc(ms), 'side': side, 'price': price, 'size': size}
        trades = [trade(5, 300000, 'sell'), trade(4, 299999, 'sell'), trade(4, 299999, 'sell'), trade(3, 0, 'buy'), trade(2, -1, 'buy')]
        with patch('collector.get', return_value=(trades, {})):
            row = collector.coinbase(0, 300000)[0]
        self.assertEqual(row['coverage'], 'complete')
        self.assertEqual((row['buy_usd'], row['sell_usd'], row['cvd_usd'], row['taker_ratio'], row['trades']), (20, 20, 0, 1, 2))

    def test_coinbase_missing_cursor_never_claims_full_coverage(self):
        trades = [{'trade_id': 1, 'time': collector.utc(200000), 'side': 'sell', 'price': '10', 'size': '2'}]
        with patch('collector.get', return_value=(trades, {})):
            self.assertEqual(collector.coinbase(0, 300000)[0]['coverage'], 'incomplete')

    def test_coinbase_busy_window_paginates_past_80_pages_without_inventing_coverage(self):
        pages = []
        for page in range(84):
            ident = 200 - page
            trade = {'trade_id': ident, 'time': collector.utc((82 - page) * 5000),
                     'side': 'sell' if page % 2 else 'buy', 'price': '10', 'size': '2'}
            pages.append(([trade], {'cb-after': str(ident)}))
        with patch('collector.get', side_effect=pages), patch('collector.time.sleep'):
            row = collector.coinbase(0, 300000)[0]
        self.assertEqual(row['coverage'], 'complete')
        self.assertEqual((row['buy_usd'], row['sell_usd'], row['cvd_usd'], row['trades']), (600, 600, 0, 60))
        self.assertEqual(collector.FETCH_STATS['coinbase']['pages'], 84)
        with patch('collector.get', side_effect=pages), patch('collector.time.sleep'), patch('collector.COINBASE_MAX_PAGES', 80):
            row = collector.coinbase(0, 300000)[0]
        self.assertEqual(row['coverage'], 'incomplete')
        self.assertEqual(row['reason'], 'pagination limit reached')
        self.assertIsNone(row['cvd_usd'])

    def test_coinbase_rejects_unknown_side(self):
        trades = [{'trade_id': 1, 'time': collector.utc(200000), 'side': 'unknown', 'price': '10', 'size': '2'}]
        with patch('collector.get', return_value=(trades, {})), self.assertRaises(ValueError):
            collector.coinbase(0, 300000)

    def test_binance_missing_candle_only_invalidates_affected_interval(self):
        candles = [[t, 0, 0, 0, 0, 0, t+59999, '100', 4, 0, '60', 0] for t in range(0, 600000, 60000) if t != 60000]
        with patch('collector.get', return_value=(candles, {})):
            rows = collector.binance(0, 600000)
        self.assertEqual([r['coverage'] for r in rows], ['incomplete', 'complete'])
        self.assertEqual(rows[1]['cvd_usd'], 100)

    def test_one_gap_keeps_hour_incomplete_and_preserves_recent_15m(self):
        rows = [collector.entry(exchange, t, t+300000, 100, 50, 1, 'complete') for exchange in ('binance', 'coinbase') for t in range(0, 3600000, 300000)]
        rows = [r for r in rows if not (r['exchange'] == 'coinbase' and r['start_utc'] == collector.utc(0))]
        windows = collector.windows_for(rows, 3600000)
        self.assertEqual(windows['coinbase']['15m']['coverage'], 'complete')
        self.assertEqual(windows['coinbase']['60m']['coverage'], 'incomplete')
        self.assertIsNone(windows['coinbase']['60m']['cvd_usd'])
        self.assertEqual(windows['binance']['60m']['cvd_usd'], 600)

    def test_windows_include_start_and_exclude_end_and_older_rows(self):
        end=7200000
        for minutes in (15,60):
            with self.subTest(minutes=minutes):
                start=end-minutes*60000
                rows=[collector.entry('binance',t,t+300000,100,50,1,'complete') for t in range(start,end,300000)]
                rows.extend(collector.entry('binance',t,t+300000,1000000,1,1,'complete') for t in (start-300000,end,end+300000))
                window=collector.windows_for(rows,end)['binance'][str(minutes)+'m']
                self.assertEqual(window['coverage'],'complete')
                self.assertEqual(window['intervals'],minutes//5)
                self.assertEqual(window['buy_usd'],100*(minutes//5))
                self.assertEqual(window['cvd_usd'],50*(minutes//5))

    def test_windows_recognize_equivalent_utc_timestamp_formats(self):
        rows=[collector.entry('coinbase',t,t+300000,100,50,1,'complete') for t in range(0,3600000,300000)]
        for row in rows:
            row['start_utc']=row['start_utc'].replace('Z','+00:00')
            row['end_utc']=row['end_utc'].replace('Z','+00:00')
        windows=collector.windows_for(rows,3600000)['coinbase']
        self.assertEqual(windows['15m']['coverage'],'complete')
        self.assertEqual(windows['60m']['coverage'],'complete')

    def test_windows_reject_interval_with_wrong_duration(self):
        rows=[collector.entry('binance',t,t+300000,100,50,1,'complete') for t in range(0,900000,300000)]
        rows[-1]['end_utc']=collector.utc(1200000)
        window=collector.windows_for(rows,900000)['binance']['15m']
        self.assertEqual(window['coverage'],'incomplete')
        self.assertIsNone(window['cvd_usd'])

    def test_save_retains_history_beyond_12000_rows(self):
        rows=[collector.entry('binance',t,t+300000,100,50,1,'complete') for t in range(0,12001*300000,300000)]
        with tempfile.TemporaryDirectory() as directory, patch('collector.DATA',Path(directory)):
            collector.save(rows)
            collector.save([collector.entry('binance',12001*300000,12002*300000,100,50,1,'complete')])
            with (Path(directory)/'history.csv').open(newline='') as handle:
                saved=list(csv.DictReader(handle))
        self.assertEqual(len(saved),12002)
        self.assertEqual(saved[0]['start_utc'],rows[0]['start_utc'])
        self.assertTrue({r['start_utc'] for r in rows}<={r['start_utc'] for r in saved})

    def test_save_keeps_complete_history_when_backfill_fails(self):
        good=collector.entry('binance',0,300000,100,50,1,'complete')
        failed=collector.entry('binance',0,300000,0,0,0,'unavailable','API error')
        with tempfile.TemporaryDirectory() as directory, patch('collector.DATA',Path(directory)):
            collector.save([good])
            collector.save([failed])
            with (Path(directory)/'history.csv').open(newline='') as handle:
                saved=list(csv.DictReader(handle))
        self.assertEqual(saved[0]['coverage'],'complete')
        self.assertEqual(float(saved[0]['cvd_usd']),50)


if __name__ == '__main__':
    unittest.main()
