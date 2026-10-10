// Free-tier external read-time gate; no exchange keys or GitHub write token.
const LATEST = 'https://raw.githubusercontent.com/devanhaozhen-ai/lfm2-spot/main/data/latest.json';

export function refreshQuality(source, nowMs = Date.now()) {
  const data = structuredClone(source);
  const timestamp = value => {
    if (typeof value !== 'string' || !/(Z|[+-]\d{2}:\d{2})$/.test(value)) throw new Error('Invalid UTC timestamp');
    const time = Date.parse(value);
    if (!Number.isFinite(time)) throw new Error('Invalid timestamp');
    return time;
  };
  const generatedAge = (nowMs - timestamp(data.generated_utc)) / 1000;
  const marketAge = (nowMs - timestamp(data.latest_closed_5m_end_utc)) / 1000;
  let complete = Array.isArray(data.last_intervals) && data.last_intervals.length === 2 && data.last_intervals.every(row => row.coverage === 'complete');
  complete = complete && data.errors && Object.keys(data.errors).length === 0;
  data.missing_intervals = {};
  for (const exchange of ['binance', 'coinbase']) {
    data.missing_intervals[exchange] = {};
    for (const frame of ['15m', '60m']) {
      const window = data.markets[exchange][frame];
      const missing = window.missing_intervals || [];
      complete = complete && window.coverage === 'complete' && missing.length === 0;
      data.missing_intervals[exchange][frame] = missing;
    }
  }
  for (const history of Object.values(data.history_quality || {})) {
    complete = complete && history.coverage === 'complete' && history.missing_count === 0;
  }
  data.data_age_seconds = Math.round(Math.max(0, generatedAge) * 1000) / 1000;
  data.market_data_age_seconds = Math.round(Math.max(0, marketAge) * 1000) / 1000;
  data.stale_after_seconds = 900;
  data.quality_evaluated_utc = new Date(nowMs).toISOString();
  data.quality_status = generatedAge > 900 || marketAge > 900 ? 'stale' : complete && generatedAge >= -60 && marketAge >= -60 ? 'ok' : 'incomplete';
  data.monitoring_eligible = data.quality_status === 'ok';
  return data;
}

async function readLatest() {
  const response = await fetch(LATEST + '?external_health=' + Date.now(), {
    headers: {'Accept': 'application/json', 'Cache-Control': 'no-cache'},
    signal: AbortSignal.timeout(15000),
  });
  if (!response.ok) throw new Error('Public latest.json HTTP ' + response.status);
  return refreshQuality(await response.json());
}

function reply(data, status) {
  return new Response(JSON.stringify(data), {status, headers: {'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store', 'Access-Control-Allow-Origin': '*'}});
}

export default {
  async fetch(request) {
    const path = new URL(request.url).pathname;
    if (request.method !== 'GET') return reply({error: 'Method not allowed'}, 405);
    if (!['/health', '/latest.json'].includes(path)) return reply({error: 'Use /health or /latest.json'}, 404);
    try {
      const data = await readLatest();
      return reply(data, path === '/health' && !data.monitoring_eligible ? 503 : 200);
    } catch (error) {
      return reply({quality_evaluated_utc: new Date().toISOString(), quality_status: 'unavailable', monitoring_eligible: false, error: error.name}, 503);
    }
  },
  async scheduled() {
    const data = await readLatest();
    console.log(JSON.stringify({checked_utc: data.quality_evaluated_utc, quality_status: data.quality_status, data_age_seconds: data.data_age_seconds, market_data_age_seconds: data.market_data_age_seconds}));
    if (!data.monitoring_eligible) throw new Error('LFM2 source is ' + data.quality_status);
  },
};
