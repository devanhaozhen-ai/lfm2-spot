import copy
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch
import lfm32
import lfm32_alternatives as alt
from lfm32_sources import normalize
from lfm32_schedule import evaluate, inspect
from collector import utc
from test_lfm32 import FakeClient, START, END, trade


def client(rows): return FakeClient([({'code':'0','data':rows},{})])


def observation():
    config=lfm32.load_config();config['dynamic_min_samples']=1
    markets={}
    times=[START-1,START,END-900000,END-600000,END-300000,END-100000,END]
    for e in ('binance','coinbase'):
        rows=[]
        for i,t in enumerate(times,1):
            r=trade(i,t,False,size='2' if i in (5,6) else '1',exchange=e)
            r['p' if e=='binance' else 'price']=str(100000+i)
            rows.append(normalize(e,r))
        stream={'trades':rows,'id_gaps':0,'fill_id_gaps':False,'reversed_time':False}
        markets[e]=lfm32.build_market(e,stream,START,END,config)
    return {'generated_utc':utc(END+1000),'window_start_utc':utc(START),'window_end_utc':utc(END),
            'config':config,'markets':markets,'derivatives':{},'alternative_derivatives':{},'order_books':{},'errors':{}}


class AlternativeTests(unittest.TestCase):
    def test_oi_coin_and_usd_columns_are_not_contracts_or_usdt(self):
        rows=[[str(t),str(1000+i),str(10+i),'900000'] for i,t in enumerate(range(START,END+1,300000))]
        c=client(rows);r=alt.fetch_oi(c,END)
        self.assertEqual(r['open_interest_btc'],'22');self.assertEqual(r['open_interest_contracts'],'1012')
        self.assertEqual(r['changes']['5m']['delta_btc'],1);self.assertEqual(r['changes']['15m']['delta_btc'],3)
        self.assertEqual(r['changes']['60m']['delta_btc'],12)
        self.assertEqual(r['reported_notional_currency'],'USD');self.assertEqual(r['quote_currency'],'USDT')
        self.assertFalse(r['replaces_binance']);self.assertFalse(r['used_in_full_buy_evidence_score'])

    def test_oi_missing_endpoint_remains_null_and_partial_current_ignored(self):
        r=alt.fetch_oi(client([[str(END),'100','1','80000'],[str(END+300000),'200','2','160000']]),END)
        self.assertEqual(r['as_of_utc'],utc(END));self.assertIsNone(r['changes']['5m']['delta_btc'])

    def test_actual_funding_uses_realized_rate_and_source_timestamp(self):
        r=alt.fetch_funding(client([{'instId':alt.INSTRUMENT,'fundingTime':str(START),'fundingRate':'0.9','realizedRate':'-0.00002'}]))
        self.assertEqual(r['rate'],'-0.00002');self.assertEqual(r['actual_funding_utc'],utc(START))
        self.assertEqual(r['source_rate_field'],'realizedRate')

    def test_funding_predicted_only_does_not_become_settled(self):
        with self.assertRaises(KeyError):alt.fetch_funding(client([{'instId':alt.INSTRUMENT,'fundingTime':str(START),'fundingRate':'0.9'}]))

    def test_taker_btc_flow_side_order_and_unclosed_bar_exclusion(self):
        rows=[[str(t),'1','2'] for t in range(START,END,300000)]+[[str(END),'999999','999999']]
        c=client(rows);r=alt.fetch_flow(c,START,END)
        self.assertEqual(c.calls[0][1]['unit'],'0');self.assertEqual(r['volume_currency'],'BTC')
        self.assertEqual(r['windows']['60m']['buy_btc'],24);self.assertEqual(r['windows']['60m']['sell_btc'],12)
        self.assertEqual(r['windows']['15m']['cvd_btc'],3);self.assertNotIn('buy_quote',r['windows']['5m'])

    def test_taker_missing_bar_not_zero(self):
        rows=[[str(t),'1','2'] for t in range(START,END,300000) if t!=END-300000]
        r=alt.fetch_flow(client(rows),START,END)
        self.assertIsNone(r['windows']['5m']['buy_btc']);self.assertEqual(len(r['windows']['60m']['missing_intervals']),1)

    def test_api_error_duplicate_and_negative_volumes_rejected(self):
        with self.assertRaises(ValueError):alt.fetch_oi(FakeClient([({'code':'50011','data':[]},{})]),END)
        for rows in ([[str(START),'1','2'],[str(START),'1','2']],[[str(START),'-1','2']]):
            with self.assertRaises(ValueError):alt.fetch_flow(client(rows),START,END)

    def test_stale_alternative_data_not_refreshed_by_new_observation(self):
        context={'okx':{'components':{'oi':{'status':'available','as_of_utc':utc(END-1000000),'observed_utc':utc(END)}}}}
        self.assertFalse(alt.refresh_context(context,END)['okx']['components']['oi']['fresh_at_read'])


class ScoreAndGrainTests(unittest.TestCase):
    def setUp(self):self.clock=datetime.fromtimestamp(END/1000,tz=timezone.utc)

    def test_complete_spot_rubric_is_separate_and_full_score_stays_null(self):
        d=lfm32.refresh_quality(observation(),self.clock)
        s=d['spot_only_evidence'];self.assertIsNone(d['analysis']['buy_evidence_score_0_100'])
        for r in s['per_venue'].values():self.assertEqual(r['score_0_100'],100);self.assertEqual(r['available_weight'],100)
        self.assertIsNone(s['cross_exchange_composite_0_100']);self.assertFalse(s['institutional_probability'])
        self.assertFalse(s['predictive_use_eligible']);self.assertFalse(s['cross_exchange_score_comparison_eligible'])
        self.assertIn('spot_large_flow',d['analysis']['missing_dimensions'])

    def test_missing_dynamic_dimension_not_zero_or_rescaled(self):
        d=observation();d['markets']['coinbase']['dynamic']['windows']=None
        d=lfm32.refresh_quality(d,self.clock);s=d['spot_only_evidence']['per_venue']
        self.assertIsNone(s['coinbase']['score_0_100']);self.assertEqual(s['coinbase']['available_weight'],75)
        self.assertEqual(s['binance']['score_0_100'],100)

    def test_read_time_stale_scores_are_removed_even_when_stored_full(self):
        d=lfm32.refresh_quality(observation(),self.clock)
        d=lfm32.refresh_quality(d,self.clock+timedelta(seconds=901))
        for r in d['spot_only_evidence']['per_venue'].values():self.assertIsNone(r['score_0_100']);self.assertIsNone(r['observed_points'])

    def test_aggregate_group_never_implies_single_large_match(self):
        first=trade(1,START-1);middle=trade(2,START,size='3');middle['f']=2;middle['l']=4
        last=trade(3,END);last['f']=last['l']=5
        rows=[normalize('binance',r) for r in (first,middle,last)]
        w=lfm32.aggregate({'trades':rows},START,END,lfm32.load_config())
        self.assertEqual(w['record_count'],1);self.assertEqual(w['underlying_match_count'],3)
        self.assertEqual(w['large_record_count'],1);self.assertIsNone(w['large_individual_match_count'])
        self.assertIsNone(w['unique_taker_order_count']);self.assertEqual(w['large_amount_basis'],'aggregate_group_notional')

    def test_match_count_and_no_cross_venue_large_record_comparison(self):
        d=lfm32.refresh_quality(observation(),self.clock)
        w=d['markets']['coinbase']['windows']['15m']
        self.assertEqual(w['record_count'],w['underlying_match_count']);self.assertEqual(w['large_record_count'],w['large_individual_match_count'])
        self.assertFalse(d['analysis']['exchange_agreement']['large_record_comparison_eligible'])

    def test_alternative_success_does_not_fill_binance_fields(self):
        d=observation();d['derivatives']={'oi':{'status':'unavailable','error':'451'}}
        d['alternative_derivatives']={'okx':{'components':{'oi':{'status':'available','as_of_utc':utc(END)}}}}
        d=lfm32.refresh_quality(d,self.clock)
        self.assertEqual(d['derivatives']['oi']['status'],'unavailable');self.assertIn('derivatives',d['analysis']['missing_dimensions'])

    def test_compact_summary_keeps_required_dynamic_spot_evidence(self):
        d=observation();s=copy.deepcopy(d)
        for m in s['markets'].values():m['dynamic']['windows']={'15m':m['dynamic']['windows']['15m']}
        self.assertEqual(lfm32.refresh_quality(d,self.clock)['spot_only_evidence'],lfm32.refresh_quality(s,self.clock)['spot_only_evidence'])


class ScheduleAuditTests(unittest.TestCase):
    def setUp(self):
        self.clock=datetime(2026,10,25,7,0,tzinfo=timezone.utc);self.now=int(self.clock.timestamp()*1000)
        self.latest={'generated_utc':utc(self.now-60000),'window_end_utc':utc(self.now-300000),'collection_mode':'scheduled','collection_run_id':'3'}
        self.runs=[{'id':i,'event':'schedule','head_branch':'main','created_at':utc(self.now-(4-i)*300000),'conclusion':'success'} for i in (1,2,3)]
        self.jobs=[{'name':'lfm32','status':'completed','conclusion':'success'}]

    def report(self,runs=None,latest=None):return evaluate(latest or self.latest,runs if runs is not None else self.runs,latest_jobs=self.jobs,now=self.clock)

    def test_three_real_scheduled_runs_can_verify_cadence(self):
        r=self.report();self.assertTrue(r['continuous_5m_verified']);self.assertEqual(r['status'],'ok')
        self.assertTrue(r['publication_event_verified']);self.assertEqual(r['max_observed_gap_seconds'],300)

    def test_manual_runs_cannot_be_counted_as_schedule(self):
        runs=[{**r,'event':'workflow_dispatch'} for r in self.runs]
        r=self.report(runs,{**self.latest,'collection_mode':'manual_workflow_dispatch'})
        self.assertFalse(r['continuous_5m_verified']);self.assertEqual(r['scheduled_runs_seen_last_24h'],0)
        self.assertEqual(r['publication_actual_event'],'workflow_dispatch')

    def test_sparse_schedule_and_old_data_are_degraded(self):
        runs=[{**r,'created_at':utc(self.now-(5-i)*3600000)} for i,r in enumerate(self.runs)]
        latest={**self.latest,'generated_utc':utc(self.now-7200000),'window_end_utc':utc(self.now-7500000)}
        r=self.report(runs,latest);self.assertEqual(r['status'],'degraded');self.assertFalse(r['continuous_5m_verified'])
        self.assertIn('scheduled_run_overdue',r['problems']);self.assertIn('published_data_stale_or_future',r['problems'])

    def test_workflow_failure_is_separate_from_lfm32_success(self):
        self.runs[-1]['conclusion']='failure';r=self.report()
        self.assertEqual(r['latest_scheduled_run']['conclusion'],'failure');self.assertEqual(r['latest_scheduled_lfm32_job']['conclusion'],'success')
        self.assertTrue(r['continuous_5m_verified'])

    def test_mislabeled_manual_event_rejected(self):
        self.runs[-1]['event']='workflow_dispatch';r=self.report()
        self.assertFalse(r['publication_event_verified']);self.assertIn('publication_event_unverified_or_mismatched',r['problems'])

    def test_failed_job_cannot_verify_cadence(self):
        self.jobs[0]['conclusion']='failure';self.assertFalse(self.report()['continuous_5m_verified'])

    def test_fetch_uses_official_publication_run_and_job_records(self):
        calls=[]
        def fetch(path):
            calls.append(path)
            if path.startswith('actions/workflows'):return {'workflow_runs':self.runs}
            if path.endswith('/jobs'):return {'jobs':self.jobs}
            return self.runs[-1]
        r=inspect(self.latest,fetch,self.clock)
        self.assertTrue(r['publication_event_verified']);self.assertIn('actions/runs/3',calls);self.assertIn('actions/runs/3/jobs',calls)


if __name__=='__main__':unittest.main()
