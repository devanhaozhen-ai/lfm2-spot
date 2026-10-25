"""运行 LFM3.2: read commit-pinned data and recalculate quality; optional manual refresh.

A successful Actions run is not a claim that unavailable sources are now usable.
Never execute trades or create paid resources.
"""
import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from lfm32 import refresh_quality
from work_collect import api, read_contents


def read_latest(ref='main'):
    commit = api('commits?sha='+ref+'&path=data/lfm32_latest.json&per_page=1')[0]['sha']
    data = refresh_quality(json.loads(read_contents('data/lfm32_latest.json',commit)))
    return commit, data


def refresh(ref='main', timeout=900):
    request_id='lfm32-'+uuid.uuid4().hex
    triggered = datetime.now(timezone.utc)
    api('actions/workflows/collect.yml/dispatches','-X','POST','-f','ref='+ref,'-f','inputs[request_id]='+request_id)
    deadline=time.monotonic()+timeout
    # Match request identifier AND branch; legacy job failure is reported separately.
    run=None
    while time.monotonic()<deadline:
        runs=api('actions/workflows/collect.yml/runs?event=workflow_dispatch&per_page=30')['workflow_runs']
        run=next((r for r in runs if r['display_title']=='BTC collection '+request_id and r['head_branch']==ref),None)
        if run: break
        time.sleep(5)
    if not run: raise TimeoutError('manual dispatch not registered')
    while time.monotonic()<deadline:
        run=api('actions/runs/'+str(run['id']))
        if run['status']=='completed': break
        time.sleep(10)
    if run['status']!='completed':raise TimeoutError('manual workflow timed out')
    jobs=api('actions/runs/'+str(run['id'])+'/jobs')['jobs']
    job=next((j for j in jobs if j['name']=='lfm32'),None)
    if not job or job['conclusion']!='success':raise RuntimeError('LFM3.2 collection failed: '+run['html_url'])
    commit,data=read_latest(ref)
    if data['request_id']!=request_id or str(data['collection_run_id'])!=str(run['id']):raise ValueError('published output does not match manual request')
    if datetime.fromisoformat(data['generated_utc'].replace('Z','+00:00'))<triggered:raise ValueError('old output cannot satisfy new request')
    return commit,data,run


def main():
    p=argparse.ArgumentParser();p.add_argument('--refresh',action='store_true');p.add_argument('--ref',default='main');args=p.parse_args()
    if args.refresh: commit,data,run=refresh(args.ref)
    else: commit,data=read_latest(args.ref);run=None
    result={'commit':commit,'workflow':run['html_url'] if run else None,'workflow_conclusion':run['conclusion'] if run else None,
            'quality_status':data['quality_status'],'monitoring_eligible':data['monitoring_eligible'],
            'generated_utc':data['generated_utc'],'window_end_utc':data['window_end_utc'],'analysis':data['analysis'],
            'markets':data['markets'],'derivatives':data['derivatives'],'order_books':data['order_books'],'errors':data['errors']}
    print(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False))
    return 0 if data['monitoring_eligible'] else 2


if __name__=='__main__':raise SystemExit(main())
