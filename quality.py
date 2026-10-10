"""Re-evaluate freshness at read time: a static JSON cannot age itself."""
import copy
from datetime import datetime, timezone

STALE_AFTER_SECONDS = 900


def parse_time(value):
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('timestamp must include a UTC offset')
    return parsed


def refresh_quality(data, now=None):
    data = copy.deepcopy(data)
    now = now or datetime.now(timezone.utc)
    age = (now - parse_time(data['generated_utc'])).total_seconds()
    market_age = (now - parse_time(data['latest_closed_5m_end_utc'])).total_seconds()
    data['data_age_seconds'] = round(max(0, age), 3)
    data['market_data_age_seconds'] = round(max(0, market_age), 3)
    data['stale_after_seconds'] = STALE_AFTER_SECONDS
    data['quality_evaluated_utc'] = now.isoformat().replace('+00:00', 'Z')
    data['missing_intervals'] = {exchange: {frame: data['markets'][exchange][frame].get('missing_intervals', []) for frame in ('15m', '60m')} for exchange in ('binance', 'coinbase')}
    complete = all(data['markets'][exchange][frame]['coverage'] == 'complete' and not data['markets'][exchange][frame].get('missing_intervals') for exchange in ('binance', 'coinbase') for frame in ('15m', '60m')) and len(data['last_intervals']) == 2 and all(row['coverage'] == 'complete' for row in data['last_intervals']) and not data['errors']
    complete = complete and all(row['coverage'] == 'complete' and not row['missing_count'] for row in data.get('history_quality', {}).values())
    stale = age > STALE_AFTER_SECONDS or market_age > STALE_AFTER_SECONDS
    data['quality_status'] = 'stale' if stale else 'ok' if complete and age >= -60 and market_age >= -60 else 'incomplete'
    data['monitoring_eligible'] = data['quality_status'] == 'ok'
    return data
