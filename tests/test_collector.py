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

    def test_coinbase_rejects_unknown_side(self):
        trades = [{'trade_id': 1, 'time': collector.utc(200000), 'side': 'unknown', 'price': '10', 'size': '2'}]
        with patch('collector.get', return_value=(trades, {})), self.assertRaises(ValueError):
            collector.coinbase(0, 300000)

    def test_binance_missing_candle_only_invalidates_affected_interval(self):
        candles = [[t, 0, 0, 0, 0, 0, t+59999, '100', 4, 0, '60', 0] for t in range(0, 600000, 60000) if t != 60000]
        with patch('collector.get', return_value=(candles, {})):
            rows = collector.binance(0, 600000)
        self.assertEqual([r['coverage'] for r in rows], ['unavailable', 'complete'])
        self.assertEqual(rows[1]['cvd_usd'], 100)

    def test_one_gap_keeps_hour_incomplete_and_preserves_recent_15m(self):
        rows = [collector.entry(exchange, t, t+300000, 100, 50, 1, 'complete') for exchange in ('binance', 'coinbase') for t in range(0, 3600000, 300000)]
        rows = [r for r in rows if not (r['exchange'] == 'coinbase' and r['start_utc'] == collector.utc(0))]
        windows = collector.windows_for(rows, 3600000)
        self.assertEqual(windows['coinbase']['15m']['coverage'], 'complete')
        self.assertEqual(windows['coinbase']['60m']['coverage'], 'incomplete')
        self.assertIsNone(windows['coinbase']['60m']['cvd_usd'])
        self.assertEqual(windows['binance']['60m']['cvd_usd'], 600)


if __name__ == '__main__':
    unittest.main()
