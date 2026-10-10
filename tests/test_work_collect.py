import copy
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch
from quality import refresh_quality
from verify_sources import compare
from work_collect import next_window, require_new_data, preserve_history, WindowNotCurrent


class WorkCollectionTests(unittest.TestCase):
    def before(self):
        return {'generated_utc': '2026-10-10T14:30:10Z', 'latest_closed_5m_end_utc': '2026-10-10T14:30:00Z'}

    def data(self):
        return {'generated_utc': '2026-10-10T14:45:10Z', 'latest_closed_5m_end_utc': '2026-10-10T14:45:00Z', 'request_id': 'request', 'collection_run_id': '123', 'collection_mode': 'manual_workflow_dispatch', 'errors': {}, 'last_intervals': [{'coverage': 'complete'}, {'coverage': 'complete'}], 'markets': {e: {f: {'coverage': 'complete', 'intervals': n, 'expected': n, 'missing_intervals': []} for f, n in (('15m', 3), ('60m', 12))} for e in ('binance', 'coinbase')}}

    def check(self, data):
        return require_new_data(data, self.before(), 'request', 123, datetime(2026,10,10,14,45,1,tzinfo=timezone.utc), datetime(2026,10,10,14,46,tzinfo=timezone.utc))

    def test_verified_new_request_can_be_used(self):
        self.assertTrue(self.check(self.data())['monitoring_eligible'])

    def test_other_successful_requests_cannot_be_substituted(self):
        data = self.data(); data['request_id'] = 'older-request'
        with self.assertRaisesRegex(ValueError, 'does not belong'): self.check(data)

    def test_new_json_timestamp_without_window_advance_is_rejected(self):
        data = self.data(); data['latest_closed_5m_end_utc'] = self.before()['latest_closed_5m_end_utc']
        with self.assertRaisesRegex(ValueError, 'did not advance'): self.check(data)

    def test_crossing_boundary_requires_another_collection(self):
        data = self.data(); data['latest_closed_5m_end_utc'] = '2026-10-10T14:40:00Z'
        with self.assertRaises(WindowNotCurrent): self.check(data)

    def test_generated_before_trigger_cannot_claim_success(self):
        data = self.data(); data['generated_utc'] = '2026-10-10T14:44:00Z'
        with self.assertRaisesRegex(ValueError, 'after this trigger'): self.check(data)

    def test_complete_label_with_wrong_interval_count_is_rejected(self):
        data = self.data(); data['markets']['coinbase']['60m']['intervals'] = 11
        with self.assertRaisesRegex(ValueError, 'coverage check failed'): self.check(data)

    def test_repeated_collection_waits_for_new_real_closed_window(self):
        clock = datetime(2026,10,10,14,46,tzinfo=timezone.utc)
        expected = int(datetime(2026,10,10,14,50,tzinfo=timezone.utc).timestamp())
        self.assertEqual(next_window(self.data(), clock), expected)

    def test_manual_idle_gaps_remain_visible_but_do_not_invalidate_complete_current_hour(self):
        data = self.data(); data['history_quality'] = {'coinbase': {'coverage': 'incomplete', 'missing_count': 180}}
        checked = self.check(data)
        self.assertTrue(checked['monitoring_eligible'])
        self.assertEqual(checked['history_quality']['coinbase']['missing_count'], 180)
        data['markets']['coinbase']['60m']['coverage'] = 'incomplete'
        with self.assertRaises(ValueError): self.check(data)

    def test_no_old_data_success_on_failed_actions(self):
        from work_collect import wait_run
        with patch('work_collect.api', side_effect=[{'id': 1, 'status': 'completed', 'conclusion': 'failure', 'html_url': 'https://example.test'}, {'jobs': [{'steps': [{'name': 'Collect market data', 'conclusion': 'failure'}]}]}]):
            with self.assertRaisesRegex(RuntimeError, 'Collect market data'): wait_run({'id': 1}, float('inf'))

    def test_source_recomputation_rejects_reversed_taker_side(self):
        row = {'coverage': 'complete', 'trades': 3, 'buy_usd': 20, 'sell_usd': 100, 'cvd_usd': -80, 'taker_ratio': .2}
        with self.assertRaisesRegex(ValueError, 'buy_usd mismatch'): compare(row, Decimal(100), Decimal(20), 3)

    def test_changed_existing_history_is_rejected(self):
        before = b'exchange,start_utc,coverage,buy_usd\nbinance,2026-10-10T14:00:00Z,complete,100\n'
        with self.assertRaises(ValueError): preserve_history(before, before.replace(b',100', b',999'))

    def test_lost_incomplete_history_is_also_rejected(self):
        before = b'exchange,start_utc,coverage,buy_usd\nbinance,2026-10-10T14:00:00Z,incomplete,\n'
        with self.assertRaises(ValueError): preserve_history(before, before.splitlines()[0] + b'\n')
