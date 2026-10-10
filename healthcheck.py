"""Independent, read-only market check. Only its own health report is written."""
import argparse
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from collector import atomic_write
from quality import parse_time, refresh_quality

REPOSITORY = 'devanhaozhen-ai/lfm2-spot'
LATEST_URL = f'https://raw.githubusercontent.com/{REPOSITORY}/main/data/latest.json'
RUNS_URL = f'https://api.github.com/repos/{REPOSITORY}/actions/workflows/collect.yml/runs?per_page=10'


def fetch_json(url, token=None):
    headers = {'User-Agent': 'LFM2-Independent-Health/2.0', 'Accept': 'application/json', 'Cache-Control': 'no-cache'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    for attempt in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=15) as response:
                return json.load(response)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
            if attempt:
                raise
            time.sleep(1)


def evaluate(data, runs, previous=None, now=None):
    now = now or datetime.now(timezone.utc)
    checked = refresh_quality(data, now)
    problems = []
    if checked['quality_status'] != 'ok':
        problems.append(checked['quality_status'])
    completed = next((run for run in runs if run['status'] == 'completed'), None)
    if completed and completed['conclusion'] != 'success':
        problems.append('collector_failed')
    previous = previous or {}
    same = previous.get('generated_utc') == checked['generated_utc']
    unchanged_since = previous.get('unchanged_since_utc', checked['generated_utc']) if same else checked['generated_utc']
    unchanged_seconds = max(0, (now - parse_time(unchanged_since)).total_seconds())
    if unchanged_seconds > 900 and 'stale' not in problems:
        problems.append('not_updated')
    return {
        'checked_utc': now.isoformat().replace('+00:00', 'Z'),
        'health_status': 'failed' if problems else 'ok',
        'problems': problems,
        'quality_status': checked['quality_status'],
        'monitoring_eligible': checked['monitoring_eligible'] and not problems,
        'generated_utc': checked['generated_utc'],
        'latest_closed_5m_end_utc': checked['latest_closed_5m_end_utc'],
        'data_age_seconds': checked['data_age_seconds'],
        'market_data_age_seconds': checked['market_data_age_seconds'],
        'missing_intervals': checked['missing_intervals'],
        'history_quality': checked.get('history_quality', {}),
        'source_errors': checked['errors'],
        'unchanged_since_utc': unchanged_since,
        'unchanged_seconds': round(unchanged_seconds, 3),
        'consecutive_unchanged_checks': previous.get('consecutive_unchanged_checks', 0) + 1 if same else 0,
        'latest_completed_collector': {key: completed.get(key) for key in ('id', 'event', 'created_at', 'updated_at', 'conclusion', 'html_url')} if completed else None,
        'actions_check': 'checked' if runs else 'unavailable_or_skipped',
        'scheduler_dependency': 'GitHub Actions; independent collector job, shared GitHub scheduler',
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path(__file__).parent / 'data/health.json')
    parser.add_argument('--latest-path', type=Path)
    parser.add_argument('--runs-path', type=Path)
    parser.add_argument('--skip-actions-check', action='store_true')
    args = parser.parse_args()
    previous = json.loads(args.output.read_text()) if args.output.exists() else {}
    now = datetime.now(timezone.utc)
    try:
        data = json.loads(args.latest_path.read_text()) if args.latest_path else fetch_json(LATEST_URL + '?healthcheck=' + str(int(now.timestamp())))
        runs = [] if args.skip_actions_check else (json.loads(args.runs_path.read_text())['workflow_runs'] if args.runs_path else fetch_json(RUNS_URL, os.environ.get('GITHUB_TOKEN'))['workflow_runs'])
        report = evaluate(data, runs, previous, datetime.now(timezone.utc))
        if not args.skip_actions_check and not runs:
            report['health_status'] = 'failed'
            report['monitoring_eligible'] = False
            report['problems'].append('collector_run_missing')
    except (KeyError, TypeError, ValueError, urllib.error.URLError, TimeoutError, OSError) as error:
        report = {'checked_utc': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), 'health_status': 'failed', 'monitoring_eligible': False, 'problems': ['health_check_unavailable'], 'error_type': type(error).__name__}
    atomic_write(args.output, lambda handle: handle.write(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n'))
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    summary_path = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary_path:
        with open(summary_path, 'a') as handle:
            handle.write('LFM2 health: ' + report['health_status'] + '\n\n```json\n' + json.dumps(report, ensure_ascii=False, indent=2) + '\n```\n')
    return 0 if report['health_status'] == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
