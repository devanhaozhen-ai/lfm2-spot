import csv
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch
import collector
import healthcheck
from quality import refresh_quality


class UnattendedTests(unittest.TestCase):
    def data(self):
        return {'generated_utc': '2026-10-10T14:00:10Z', 'latest_closed_5m_end_utc': '2026-10-10T14:00:00Z', 'quality_status': 'ok', 'monitoring_eligible': True, 'errors': {}, 'last_intervals': [{'coverage': 'complete'}, {'coverage': 'complete'}], 'markets': {e: {f: {'coverage': 'complete', 'missing_intervals': []} for f in ('15m', '60m')} for e in ('binance', 'coinbase')}}

    def test_health_rejects_stored_ok_after_collector_stops(self):
        report = healthcheck.evaluate(self.data(), [], now=datetime(2026, 10, 10, 14, 16, tzinfo=timezone.utc))
        self.assertEqual(report['health_status'], 'failed')
        self.assertEqual(report['quality_status'], 'stale')
        self.assertFalse(report['monitoring_eligible'])
        self.assertEqual(report['market_data_age_seconds'], 960)

    def test_boundary_exactly_fifteen_minutes_allowed_then_one_second_blocks(self):
        boundary = datetime(2026, 10, 10, 14, 15, tzinfo=timezone.utc)
        self.assertTrue(refresh_quality(self.data(), boundary)['monitoring_eligible'])
        self.assertFalse(refresh_quality(self.data(), boundary + timedelta(seconds=1))['monitoring_eligible'])

    def test_failed_collector_is_detected_even_if_previous_data_is_fresh(self):
        runs = [{'id': 1, 'status': 'completed', 'conclusion': 'failure'}]
        report = healthcheck.evaluate(self.data(), runs, now=datetime(2026, 10, 10, 14, 1, tzinfo=timezone.utc))
        self.assertIn('collector_failed', report['problems'])
        self.assertFalse(report['monitoring_eligible'])

    def test_incomplete_source_is_independently_detected(self):
        data = self.data()
        data['markets']['coinbase']['60m']['coverage'] = 'incomplete'
        data['markets']['coinbase']['60m']['missing_intervals'] = ['2026-10-10T13:20:00Z']
        report = healthcheck.evaluate(data, [], now=datetime(2026, 10, 10, 14, 1, tzinfo=timezone.utc))
        self.assertEqual(report['quality_status'], 'incomplete')
        self.assertFalse(report['monitoring_eligible'])
        self.assertEqual(report['missing_intervals']['coinbase']['60m'], ['2026-10-10T13:20:00Z'])

    def test_unchanged_check_counter_resets_on_real_new_generation(self):
        now = datetime(2026, 10, 10, 14, 1, tzinfo=timezone.utc)
        first = healthcheck.evaluate(self.data(), [], now=now)
        second = healthcheck.evaluate(self.data(), [], first, now + timedelta(minutes=1))
        self.assertEqual(second['consecutive_unchanged_checks'], 1)
        data = self.data(); data['generated_utc'] = '2026-10-10T14:02:00Z'
        self.assertEqual(healthcheck.evaluate(data, [], second, now + timedelta(minutes=2))['consecutive_unchanged_checks'], 0)

    def test_backfill_finds_middle_hole_even_with_later_complete_data(self):
        end = 8 * 3600000
        hole = end - 3 * 3600000
        rows = [collector.entry(e, t, t + 300000, 100, 50, 1, 'complete') for e in ('binance', 'coinbase') for t in range(end - 4 * 3600000, end, 300000) if not (e == 'coinbase' and t == hole)]
        with tempfile.TemporaryDirectory() as directory, patch('collector.DATA', Path(directory)):
            collector.save(rows)
            self.assertEqual(collector.backfill_start(end), hole)
            collector.save([collector.entry('coinbase', hole, hole + 300000, 100, 50, 1, 'complete')])
            self.assertEqual(collector.backfill_start(end), end - 3600000)
            with (Path(directory) / 'history.csv').open(newline='') as handle:
                saved = list(csv.DictReader(handle))
            self.assertEqual(len(saved), len(rows) + 1)
            self.assertEqual(collector.history_quality(saved, end)['coinbase']['missing_count'], 0)

    def test_long_outage_is_visible_instead_of_hidden_by_backfill_limit(self):
        end = 10 * 3600000
        rows = [collector.entry(e, t, t + 300000, 100, 50, 1, 'complete') for e in ('binance', 'coinbase') for t in (0, end - 300000)]
        with tempfile.TemporaryDirectory() as directory, patch('collector.DATA', Path(directory)):
            collector.save(rows)
            self.assertEqual(collector.backfill_start(end), end - 6 * 3600000)
        result = collector.history_quality(rows, end)
        self.assertGreater(result['coinbase']['outside_backfill_horizon'], 0)
        data = self.data(); data['history_quality'] = result
        self.assertEqual(refresh_quality(data, datetime(2026, 10, 10, 14, 1, tzinfo=timezone.utc))['quality_status'], 'incomplete')

    def test_equivalent_timestamp_refetch_does_not_duplicate_or_overwrite(self):
        original = collector.entry('binance', 0, 300000, 100, 50, 1, 'complete')
        refetch = collector.entry('binance', 0, 300000, 999, 50, 1, 'complete')
        refetch['start_utc'] = refetch['start_utc'].replace('Z', '+00:00')
        with tempfile.TemporaryDirectory() as directory, patch('collector.DATA', Path(directory)):
            collector.save([original]); collector.save([refetch])
            with (Path(directory) / 'history.csv').open(newline='') as handle:
                rows = list(csv.DictReader(handle))
        self.assertEqual(len(rows), 1)
        self.assertEqual(float(rows[0]['buy_usd']), 100)


if __name__ == '__main__':
    unittest.main()
