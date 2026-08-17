/**
 * loadtest/k6_gateway_stress.js
 *
 * Document 06 Phase 5 build task: "Send 5,000+ concurrent requests
 * through the gateway with mixed team keys, models, and priorities.
 * Measure: gateway overhead latency (should be <10ms), rate limiting
 * accuracy under load, fallback behavior under simulated provider
 * outages, and dashboard accuracy."
 *
 * Three scenarios run concurrently:
 *   - gatewayTraffic:  ramping-arrival-rate through the real gateway --
 *     the thing actually being benchmarked.
 *   - baselineTraffic: the identical shape, straight at mock-providers,
 *     bypassing the gateway entirely. Without this, "<10ms gateway
 *     overhead" isn't a measurable claim -- it's an assertion. This is
 *     the same "Direct Upstream vs. Gateway Proxy" comparison the
 *     project's own reference HTML prototype benchmark chart and
 *     deploy/grafana/dashboards/performance.json's own documented
 *     caveat both call for.
 *   - chaosInjector:   a single-iteration scenario that flips
 *     mock-providers into an openai outage 1 minute into the run and
 *     clears it a minute later -- Document 06's "mid-run, mock provider
 *     is forced to 500/high-latency" chaos scenario, so the fallback
 *     and circuit-breaker Grafana panels have something to show in the
 *     same run that produces the throughput numbers.
 *
 * Run via docker-compose (recommended -- zero local install):
 *     docker compose --profile load-test run --rm k6
 *
 * Run locally against a stack already up on localhost (needs a local k6
 * binary -- https://k6.io):
 *     k6 run loadtest/k6_gateway_stress.js
 *
 * VERSION NOTE ("search first" pass, Aug 2026): k6 stable is 1.3.0
 * (grafana/k6:1.3.0 in docker-compose.yml). The `experimental-prometheus-rw`
 * output flag and its K6_PROMETHEUS_RW_* env vars are current as of this
 * pass per Grafana's own k6 documentation -- re-verify if k6 output
 * silently stops appearing in Prometheus after a k6 upgrade, since
 * "experimental" output flags are exactly the kind of thing that gets
 * renamed on a minor version bump.
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend } from 'k6/metrics';

const GATEWAY_BASE_URL = __ENV.GATEWAY_BASE_URL || 'http://localhost:8000';
const MOCK_PROVIDERS_BASE_URL = __ENV.MOCK_PROVIDERS_BASE_URL || 'http://localhost:9000';
const TEAM_API_KEY = __ENV.TEAM_API_KEY || 'sk-gw-datascience-demo-001';

// Custom Trends (in addition to k6's built-in http_req_duration) so the
// gateway and baseline scenarios' latencies can be compared directly in
// handleSummary() below, tagged separately from the mixed aggregate
// http_req_duration would otherwise produce.
const gatewayDuration = new Trend('gateway_request_duration', true);
const baselineDuration = new Trend('baseline_request_duration', true);

export const options = {
  scenarios: {
    gateway_traffic: {
      executor: 'ramping-arrival-rate',
      exec: 'gatewayRequest',
      startRate: 50,
      timeUnit: '1s',
      preAllocatedVUs: 500,
      maxVUs: 2000,
      stages: [
        { duration: '30s', target: 200 },
        { duration: '2m', target: 2000 },
        { duration: '30s', target: 0 },
      ],
    },
    baseline_traffic: {
      executor: 'ramping-arrival-rate',
      exec: 'baselineRequest',
      startRate: 50,
      timeUnit: '1s',
      preAllocatedVUs: 200,
      maxVUs: 500,
      stages: [
        { duration: '30s', target: 200 },
        { duration: '2m', target: 2000 },
        { duration: '30s', target: 0 },
      ],
    },
    chaos_injector: {
      executor: 'shared-iterations',
      exec: 'chaosCycle',
      vus: 1,
      iterations: 1,
      startTime: '1m',
    },
  },
  thresholds: {
    // Document 06's own pass/fail bar. This is END-TO-END latency
    // through the gateway (proxy + mock-upstream round trip), NOT
    // isolated gateway overhead -- same caveat performance.json's own
    // panel description already carries. handleSummary() below prints
    // the actual overhead delta (gateway p99 - baseline p99), which is
    // the number the Phase 6 narrative should quote for the literal
    // "<10ms gateway overhead" claim.
    gateway_request_duration: ['p(99)<50'],
    http_req_failed: ['rate<0.01'],
  },
};

const CHAT_BODY = JSON.stringify({
  model: 'tier-1-reasoning',
  messages: [{ role: 'user', content: 'Benchmark probe message.' }],
  stream: false,
});

const BASELINE_BODY = JSON.stringify({
  model: 'mock-gpt-5.6-sol',
  messages: [{ role: 'user', content: 'Benchmark probe message.' }],
  stream: false,
});

const JSON_HEADERS = { 'Content-Type': 'application/json' };

export function gatewayRequest() {
  const res = http.post(`${GATEWAY_BASE_URL}/v1/chat/completions`, CHAT_BODY, {
    headers: { ...JSON_HEADERS, 'X-Gateway-API-Key': TEAM_API_KEY },
    tags: { scenario: 'gateway' },
  });
  gatewayDuration.add(res.timings.duration);
  check(res, {
    'gateway: status is 200': (r) => r.status === 200,
    'gateway: served-model header present': (r) => !!r.headers['X-Gateway-Served-Model'],
  });
}

export function baselineRequest() {
  const res = http.post(`${MOCK_PROVIDERS_BASE_URL}/openai/v1/responses`, BASELINE_BODY, {
    headers: JSON_HEADERS,
    tags: { scenario: 'baseline' },
  });
  baselineDuration.add(res.timings.duration);
  check(res, { 'baseline: status is 200': (r) => r.status === 200 });
}

export function chaosCycle() {
  // 1 minute in: force every openai call to fail so gateway_traffic's
  // in-flight requests start failing over to anthropic -- watch
  // gen_ai_fallback_events_total / the Operations dashboard's circuit
  // timeline move during this window.
  http.post(
    `${MOCK_PROVIDERS_BASE_URL}/_chaos/config`,
    JSON.stringify({ provider: 'openai', error_rate: 1.0 }),
    { headers: JSON_HEADERS }
  );
  sleep(60); // hold the outage for a minute
  http.post(`${MOCK_PROVIDERS_BASE_URL}/_chaos/reset`, null, { headers: JSON_HEADERS });
}

export function handleSummary(data) {
  const gw = data.metrics.gateway_request_duration;
  const bl = data.metrics.baseline_request_duration;
  const gwP99 = gw ? gw.values['p(99)'] : NaN;
  const blP99 = bl ? bl.values['p(99)'] : NaN;
  const overheadMs = gwP99 - blP99;

  const lines = [
    '',
    '=== Gateway overhead (P99) ===',
    `  gateway p99:   ${gwP99.toFixed(2)} ms  (proxy + mock upstream round trip)`,
    `  baseline p99:  ${blP99.toFixed(2)} ms  (mock upstream only, gateway bypassed)`,
    `  overhead:      ${overheadMs.toFixed(2)} ms  -- PRD target: < 10ms`,
    '',
    'Quote the overhead line above verbatim in the Phase 6 narrative -- do',
    'not round up or restate from memory (Document 06 Phase 6 done',
    "criteria: 'every claim in the narrative is traceable to a test or",
    "load-test result produced in Phase 5').",
    '',
  ];

  return {
    stdout: lines.join('\n'),
  };
}
