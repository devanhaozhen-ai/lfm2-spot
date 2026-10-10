import {test} from 'node:test';
import assert from 'node:assert/strict';
import worker, {refreshQuality} from '../external-health/worker.mjs';

function data() {
  return {generated_utc: '2026-10-10T14:00:10Z', latest_closed_5m_end_utc: '2026-10-10T14:00:00Z', quality_status: 'ok', monitoring_eligible: true, errors: {}, last_intervals: [{coverage: 'complete'}, {coverage: 'complete'}], markets: Object.fromEntries(['binance', 'coinbase'].map(e => [e, Object.fromEntries(['15m', '60m'].map(f => [f, {coverage: 'complete', missing_intervals: []}]))]))};
}

test('stopped collector cannot retain stored ok or monitoring permission', () => {
  const input = data();
  const output = refreshQuality(input, Date.parse('2026-10-10T14:16:00Z'));
  assert.equal(output.quality_status, 'stale');
  assert.equal(output.monitoring_eligible, false);
  assert.equal(output.market_data_age_seconds, 960);
  assert.equal(input.quality_status, 'ok');
});

test('recent generation cannot disguise an old market interval', () => {
  const input = data(); input.generated_utc = '2026-10-10T14:16:00Z';
  assert.equal(refreshQuality(input, Date.parse(input.generated_utc)).quality_status, 'stale');
});

test('missing historical data blocks a fresh observation', () => {
  const input = data(); input.history_quality = {coinbase: {coverage: 'incomplete', missing_count: 1}};
  assert.equal(refreshQuality(input, Date.parse('2026-10-10T14:01:00Z')).monitoring_eligible, false);
});

test('HTTP health route returns 503 for stale raw JSON even if upstream returns 200', async () => {
  const originalFetch = globalThis.fetch;
  const originalNow = Date.now;
  Date.now = () => Date.parse('2026-10-10T14:16:00Z');
  globalThis.fetch = async () => new Response(JSON.stringify(data()));
  try {
    const response = await worker.fetch(new Request('https://example.test/health'));
    assert.equal(response.status, 503);
    assert.equal((await response.json()).monitoring_eligible, false);
    assert.equal(response.headers.get('cache-control'), 'no-store');
  } finally { globalThis.fetch = originalFetch; Date.now = originalNow; }
});

test('HTTP upstream failure is fail closed', async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => new Response('', {status: 500});
  try {
    const response = await worker.fetch(new Request('https://example.test/health'));
    assert.equal(response.status, 503);
    assert.equal((await response.json()).quality_status, 'unavailable');
  } finally { globalThis.fetch = originalFetch; }
});
