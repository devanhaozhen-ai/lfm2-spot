"""Dispatch and strictly verify one new BTC observation using existing gh auth."""
import argparse
import base64
import csv
import contextlib
import json
import subprocess
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from quality import parse_time, refresh_quality
from validate import validate
from verify_sources import verify
from lfm3 import refresh_lfm3_quality
from validate_lfm3 import validate as validate_lfm3
from verify_lfm3_sources import verify as verify_lfm3

REPOSITORY = 'devanhaozhen-ai/lfm2-spot'
UTC = timezone.utc


class WindowNotCurrent(ValueError):
    pass


def now():
    return datetime.now(UTC)


def log(message, **values):
    print(json.dumps({'utc': now().isoformat(), 'message': message, **values}), file=sys.stderr, flush=True)


def gh_json(*arguments):
    result = subprocess.run(['gh', *arguments], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError('GitHub request failed: ' + result.stderr.strip()[:600])
    return json.loads(result.stdout) if result.stdout.strip() else None


def api(path, *arguments):
    endpoint = 'repos/' + REPOSITORY + ('/' + path if path else '')
    return gh_json('api', endpoint, *arguments)


def read_contents(path, ref='main'):
    response = api('contents/' + path + '?ref=' + ref)
    if response.get('encoding') == 'base64' and response.get('content'):
        return base64.b64decode(response['content'])
    # A commit-pinned public URL supports CSV files larger than the Contents limit.
    url = 'https://raw.githubusercontent.com/' + REPOSITORY + '/' + ref + '/' + path
    with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'LFM2-Work-Manual'}), timeout=20) as handle:
        return handle.read()


def snapshot():
    commit = api('commits?path=data/latest.json&per_page=1')[0]['sha']
    data = json.loads(read_contents('data/latest.json', commit))
    return commit, data, read_contents('data/history.csv', commit)


def next_window(before, clock):
    current = int(clock.timestamp()) // 300 * 300
    previous = int(parse_time(before['latest_closed_5m_end_utc']).timestamp())
    if previous % 300 or previous > current:
        raise ValueError('invalid previous five-minute boundary')
    return max(current, previous + 300)


def wait_until(target, deadline):
    while now().timestamp() < target:
        if time.monotonic() >= deadline:
            raise TimeoutError('waiting for a newly closed five-minute window timed out')
        remaining = target - now().timestamp()
        log('Waiting for the next closed five-minute interval', remaining_seconds=round(remaining))
        time.sleep(min(30, remaining))


def find_run(request_id, deadline):
    title = 'BTC manual collection ' + request_id
    while time.monotonic() < deadline:
        runs = api('actions/workflows/collect.yml/runs?event=workflow_dispatch&per_page=30')['workflow_runs']
        matches = [run for run in runs if run['display_title'] == title and run['head_branch'] == 'main']
        if len(matches) > 1:
            raise ValueError('more than one workflow matched this request')
        if matches:
            return matches[0]
        time.sleep(5)
    raise TimeoutError('GitHub did not register this workflow_dispatch request')


def wait_run(run, deadline):
    while time.monotonic() < deadline:
        run = api('actions/runs/' + str(run['id']))
        log('Waiting for GitHub Actions', run_id=run['id'], status=run['status'])
        if run['status'] == 'completed':
            if run['conclusion'] != 'success':
                jobs = api('actions/runs/' + str(run['id']) + '/jobs')['jobs']
                failed = [step['name'] for job in jobs for step in job['steps'] if step.get('conclusion') == 'failure']
                raise RuntimeError('GitHub Actions ' + str(run['conclusion']) + ': ' + ', '.join(failed) + '; ' + run['html_url'])
            return run
        time.sleep(10)
    raise TimeoutError('GitHub Actions did not complete before the Work deadline')


def public_latest(request_id, deadline, filename='latest.json'):
    url = 'https://raw.githubusercontent.com/' + REPOSITORY + '/main/data/' + filename
    publication_deadline = min(deadline, time.monotonic() + 180)
    last_error = None
    while time.monotonic() < publication_deadline:
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={'User-Agent': 'LFM2-Work-Manual', 'Cache-Control': 'no-cache'}), timeout=20) as handle:
                data = json.load(handle)
            if data.get('request_id') == request_id:
                return data
            last_error = 'public ' + filename + ' still belongs to an earlier request'
        except (OSError, ValueError) as error:
            last_error = type(error).__name__
        log('Waiting for the public JSON to update', reason=last_error)
        time.sleep(10)
    raise RuntimeError('Public ' + filename + ' publication verification failed: ' + str(last_error))


def require_new_data(data, before, request_id, run_id, triggered, clock):
    if data.get('request_id') != request_id or str(data.get('collection_run_id')) != str(run_id):
        raise ValueError('JSON does not belong to this workflow request')
    generated = parse_time(data['generated_utc'])
    end = parse_time(data['latest_closed_5m_end_utc'])
    if generated <= parse_time(before['generated_utc']) or generated < triggered:
        raise ValueError('generated_utc did not advance after this trigger')
    if end <= parse_time(before['latest_closed_5m_end_utc']):
        raise ValueError('the closed five-minute window did not advance')
    expected = int(clock.timestamp()) // 300 * 300
    if int(end.timestamp()) != expected:
        raise WindowNotCurrent('JSON does not contain the most recent closed five-minute window')
    checked = refresh_quality(data, clock)
    if generated > clock or not checked['monitoring_eligible'] or checked['errors']:
        raise ValueError('data is stale, incomplete, future-dated or contains source errors')
    for exchange in ('binance', 'coinbase'):
        for frame, count in (('15m', 3), ('60m', 12)):
            row = checked['markets'][exchange][frame]
            if row['coverage'] != 'complete' or row['intervals'] != count or row['expected'] != count:
                raise ValueError(exchange + ' ' + frame + ' coverage check failed')
    return checked


def preserve_history(before_csv, after_csv):
    before = list(csv.DictReader(before_csv.decode().splitlines()))
    after = list(csv.DictReader(after_csv.decode().splitlines()))
    key = lambda row: (row['exchange'], parse_time(row['start_utc']).timestamp())
    index = {key(row): row for row in after}
    if len(index) != len(after) or any(index.get(key(row)) != row for row in before if row['coverage'] == 'complete'):
        raise ValueError('successful history was changed, lost or duplicated')
    if any(key(row) not in index for row in before):
        raise ValueError('previous historical interval was deleted')
    return {'before_rows': len(before), 'after_rows': len(after), 'previous_complete_rows_unchanged': True, 'previous_keys_retained': True, 'duplicate_rows': 0}


def preserve_extended_history(before_csv, after_csv, price=False):
    before = list(csv.DictReader(before_csv.decode().splitlines())) if before_csv else []
    after = list(csv.DictReader(after_csv.decode().splitlines()))
    def key(row):
        return (row['exchange'], row['timeframe'] if price else '5m', parse_time(row['start_utc']).timestamp())
    index = {key(row): row for row in after}
    if len(index) != len(after) or any(key(row) not in index or (row['coverage'] == 'complete' and index[key(row)] != row) for row in before):
        raise ValueError('LFM3 historical data changed, lost or duplicated')
    return {'before_rows':len(before), 'after_rows':len(after), 'previous_complete_rows_unchanged':True, 'duplicate_rows':0}


def collect(directory, deadline):
    identity = gh_json('api', 'user')
    repository = api('')
    if repository['default_branch'] != 'main' or not repository.get('permissions', {}).get('push'):
        raise RuntimeError('the existing GitHub authorization cannot write to main')
    log('Using existing GitHub authorization', account=identity['login'])
    before_commit, before, before_csv = snapshot()
    tree = api('git/trees/' + before_commit + '?recursive=1')['tree']
    existing = {entry['path'] for entry in tree}
    before_extended = {filename: read_contents('data/' + filename, before_commit) if 'data/' + filename in existing else b'' for filename in ('price_history.csv', 'cvd_history.csv')}
    (directory / 'before.json').write_text(json.dumps(before, indent=2) + '\n')
    (directory / 'before-history.csv').write_bytes(before_csv)
    current_baseline = before
    for attempt in range(3):
        wait_until(next_window(current_baseline, now()) + 3, deadline)
        request_id = uuid.uuid4().hex
        triggered = now()
        api('actions/workflows/collect.yml/dispatches', '--method', 'POST', '-f', 'ref=main', '-f', 'inputs[request_id]=' + request_id)
        log('Dispatched BTC collection', request_id=request_id, attempt=attempt + 1)
        run = wait_run(find_run(request_id, deadline), deadline)
        commit, data, history_csv = snapshot()
        (directory / 'latest.json').write_text(json.dumps(data, indent=2) + '\n')
        (directory / 'history.csv').write_bytes(history_csv)
        extended = {filename: read_contents('data/' + filename, commit) for filename in ('lfm3_latest.json', 'price_history.csv', 'cvd_history.csv')}
        for filename, content in extended.items():
            (directory / filename).write_bytes(content)
        lfm3 = json.loads(extended['lfm3_latest.json'])
        try:
            require_new_data(data, before, request_id, run['id'], triggered, now())
            with contextlib.redirect_stdout(sys.stderr):
                validate(directory, require_complete=True)
            history = preserve_history(before_csv, history_csv)
            published = public_latest(request_id, deadline)
            if published != data:
                raise ValueError('public latest.json differs from the verified committed snapshot')
            sources = verify(data, directory / 'history.csv')
            lfm3_validation = validate_lfm3(directory)
            extended_history = {filename: preserve_extended_history(before_extended[filename], extended[filename], price=filename == 'price_history.csv') for filename in before_extended}
            published_lfm3 = public_latest(request_id, deadline, 'lfm3_latest.json')
            if published_lfm3 != lfm3:
                raise ValueError('public LFM3 JSON differs from verified committed data')
            lfm3_sources = verify_lfm3(lfm3, directory / 'cvd_history.csv')
            lfm3_checked = refresh_lfm3_quality(published_lfm3, now())
            if not lfm3_checked['monitoring_eligible']:
                raise ValueError('LFM3 data became stale or incomplete during verification')
            checked = require_new_data(published, before, request_id, run['id'], triggered, now())
        except WindowNotCurrent:
            if attempt == 2:
                raise
            current_baseline = data
            log('A new five-minute boundary closed during verification; collecting again')
            continue
        from zoneinfo import ZoneInfo
        report = {'status': 'PASS', 'monitoring_eligible': True, 'request_id': request_id,
                  'triggered_utc': triggered.isoformat(), 'data_commit': commit,
                  'workflow': {key: run.get(key) for key in ('id', 'event', 'conclusion', 'run_started_at', 'updated_at', 'html_url')},
                  'generated_beijing': parse_time(checked['generated_utc']).astimezone(ZoneInfo('Asia/Shanghai')).isoformat(),
                  'data_delay_minutes': round(checked['market_data_age_seconds'] / 60, 3),
                  'generation_delay_minutes': round(checked['data_age_seconds'] / 60, 3),
                  'history': history, 'source_verification': sources, 'latest': checked,
                  'lfm3': {'acceptance': 'PASS' if lfm3_checked['historical_cvd_quality'] == 'complete' else 'PARTIAL',
                           'monitoring_eligible': lfm3_checked['monitoring_eligible'],
                           'validation': lfm3_validation, 'history_preservation': extended_history,
                           'source_verification': lfm3_sources,
                           'historical_cvd': {e: {k:v for k,v in lfm3_checked['markets'][e]['historical_cvd'].items() if k != 'unavailable_intervals'} for e in ('binance', 'coinbase')},
                           'public_url': 'https://raw.githubusercontent.com/' + REPOSITORY + '/main/data/lfm3_latest.json'}}
        (directory / 'latest.json').write_text(json.dumps(checked, ensure_ascii=False, indent=2) + '\n')
        (directory / 'lfm3_checked.json').write_text(json.dumps(lfm3_checked, ensure_ascii=False, indent=2) + '\n')
        return report
    raise RuntimeError('no newly closed verified observation could be collected')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output-dir', type=Path, default=Path('/tmp/lfm2-work') / uuid.uuid4().hex)
    args = parser.parse_args()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = collect(args.output_dir, time.monotonic() + 1800)
        code = 0
    except (ValueError, KeyError, TypeError, AttributeError, ArithmeticError, OSError, RuntimeError, subprocess.SubprocessError) as error:
        report = {'status': 'FAIL', 'monitoring_eligible': False, 'error': str(error), 'checked_utc': now().isoformat()}
        code = 1
    (args.output_dir / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False), flush=True)
    return code


if __name__ == '__main__':
    raise SystemExit(main())
