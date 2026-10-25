"""Read-only scheduler audit: official event/job records, not environment labels."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from collector import utc, timestamp_ms, atomic_write
from work_collect import api


def evaluate(latest, runs, publication_run=None, latest_jobs=None, now=None):
    now_ms=int((now or datetime.now(timezone.utc)).timestamp()*1000)
    scheduled=sorted((r for r in runs if r['event']=='schedule' and r['head_branch']=='main'),key=lambda r:r['created_at'])
    # State explicitly that the API history is bounded; no made-up missed runs.
    recent=[r for r in scheduled if now_ms-timestamp_ms(r['created_at'])<=86400000]
    intervals=[(timestamp_ms(b['created_at'])-timestamp_ms(a['created_at']))/1000 for a,b in zip(recent,recent[1:])]
    last=scheduled[-1] if scheduled else None
    lag=(now_ms-timestamp_ms(last['created_at']))/1000 if last else None
    pub=publication_run or next((r for r in runs if str(r['id'])==str(latest.get('collection_run_id'))),None)
    expected_mode='scheduled' if pub and pub['event']=='schedule' else 'manual_workflow_dispatch' if pub and pub['event']=='workflow_dispatch' else None
    correlation=bool(pub and str(pub['id'])==str(latest.get('collection_run_id')) and expected_mode==latest.get('collection_mode'))
    generation_age=(now_ms-timestamp_ms(latest['generated_utc']))/1000
    market_age=(now_ms-timestamp_ms(latest['window_end_utc']))/1000
    job=next((j for j in (latest_jobs or []) if j['name']=='lfm32'),None)
    continuous=bool(len(recent)>=3 and intervals and max(intervals)<=600 and lag is not None and 0<=lag<=900 and job and job.get('conclusion')=='success')
    fresh=-60<=generation_age<=900 and -60<=market_age<=900
    problems=[]
    if not continuous: problems.append('five_minute_cadence_not_verified')
    if lag is None or lag>900: problems.append('scheduled_run_overdue')
    if not fresh: problems.append('published_data_stale_or_future')
    if not correlation: problems.append('publication_event_unverified_or_mismatched')
    return {'evaluated_utc':utc(now_ms),'status':'ok' if not problems else 'degraded', 'problems':problems,
            'configured_interval_seconds':300, 'allowed_observed_gap_seconds':600,
            'continuous_5m_verified':continuous,'scheduled_runs_seen_last_24h':len(recent),
            'observed_gaps_seconds_last_24h':intervals,'max_observed_gap_seconds':max(intervals) if intervals else None,
            'latest_schedule_age_seconds':lag,'latest_scheduled_run':{k:last.get(k) for k in ('id','event','created_at','conclusion','html_url')} if last else None,
            'latest_scheduled_lfm32_job':{k:job.get(k) for k in ('name','status','conclusion','html_url')} if job else None,
            'published_generation_age_seconds':generation_age,'published_market_age_seconds':market_age,
            'publication_event_verified':correlation,'publication_actual_event':pub.get('event') if pub else None,
            'publication_collection_mode':latest.get('collection_mode'),'published_run_id':latest.get('collection_run_id'),
            'coverage_note':'bounded official main-branch run history; manual runs never counted as schedule; same GitHub scheduler has no 5m SLA'}


def inspect(latest, fetch=api, now=None):
    runs=fetch('actions/workflows/collect.yml/runs?branch=main&per_page=100')['workflow_runs']
    pub=fetch('actions/runs/'+str(latest['collection_run_id'])) if latest.get('collection_run_id') else None
    last=next((r for r in sorted(runs,key=lambda r:r['created_at'],reverse=True) if r['event']=='schedule' and r['head_branch']=='main'),None)
    jobs=fetch('actions/runs/'+str(last['id'])+'/jobs')['jobs'] if last else []
    return evaluate(latest,runs,pub,jobs,now)


def main():
    p=argparse.ArgumentParser();p.add_argument('--latest',type=Path,default=Path('data/lfm32_latest.json'));p.add_argument('--output',type=Path,default=Path('data/lfm32_schedule.json'));p.add_argument('--require-continuous',action='store_true');args=p.parse_args()
    try: report=inspect(json.loads(args.latest.read_text()))
    except Exception as error: report={'status':'unavailable','continuous_5m_verified':False,'error':str(error)}
    atomic_write(args.output,lambda h:h.write(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n'))
    print(json.dumps(report,ensure_ascii=False,indent=2))
    return 1 if args.require_continuous and report['status']!='ok' else 0


if __name__=='__main__':raise SystemExit(main())
