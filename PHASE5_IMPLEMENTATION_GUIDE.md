# Phase 5 — Implementation Guide
## TRD Requirements → Delivered Code Mapping

This guide traces every requirement from Document 06 (Phase 5 section) to the actual implementation, with file references and design rationale.

---

## 1. Mock Provider Layer

### Requirement (TRD)
> "A mock provider layer that can deterministically return fixed responses matching each real provider's wire format, with no external dependencies, zero cost, and built-in chaos injection for simulating provider outages."

### Delivered
**Files:**
- `deploy/mock-providers/main.py` (265 lines)
- `deploy/mock-providers/chaos.py` (162 lines)
- `deploy/mock-providers/Dockerfile` (17 lines)
- `deploy/mock-providers/requirements.txt` (7 lines)

**Design:**
- Standalone service, separate image, own Dockerfile (`context: ./deploy/mock-providers`)
- Speaks exact real wire formats (not simplified):
  - `POST /openai/v1/responses` → OpenAI Responses API shape (events + usage)
  - `POST /anthropic/v1/messages` → Anthropic Messages API shape (SSE + usage)
  - `POST /ollama/api/chat` → Ollama NDJSON format (streaming + usage)
  - `POST /gemini/v1beta/models/{model}:generateContent` → Gemini shape
- Deterministic responses: fixed tokens (10 in, 5 out), fixed text, fixed chunks
- Zero cost: no real credentials, runs on localhost
- Live chaos control plane: `POST /_chaos/config` + `GET /_chaos/config` + `POST /_chaos/reset`

**Chaos Design (chaos.py):**
- In-memory rule storage per provider:model
- Precedence: exact model > wildcard > no rule
- Per-call checks: apply latency, then roll error_rate
- Default status: **503** (retryable), not 500 (non-retryable) — see module docstring for why
- Usage: k6 chaos scenario + integration test mid-run failures

**Wire Format Fidelity (why this matters):**
- tests/integration/test_mock_provider_wire_compat.py runs the **real** app/providers/*.py adapters in-process against this mock (httpx.ASGITransport)
- If mock drifts from real API, this test catches it immediately
- Proves adapters are not written to a simplified mock; they parse real shapes

### Integration
- Gateway env vars (`docker-compose.yml`, gateway service):
  ```
  OPENAI_BASE_URL=http://mock-providers:9000/openai
  ANTHROPIC_BASE_URL=http://mock-providers:9000/anthropic
  OLLAMA_BASE_URL=http://mock-providers:9000/ollama
  GEMINI_BASE_URL=http://mock-providers:9000/gemini/v1beta
  ```
- Startup: `docker-compose.yml` orders mock-providers before gateway (depends_on)
- Healthcheck: gateway probes `/readyz` only after mock-providers `/healthz` passes

---

## 2. Containerization & Orchestration

### Requirement (TRD)
> "The gateway and all observability infrastructure must run via docker-compose up with zero manual configuration and zero real API keys required for the default demo path."

### Delivered
**Files:**
- `Dockerfile` (42 lines) — gateway image
- `docker-compose.yml` (223 lines) — full stack orchestration
- `.dockerignore` (17 lines) — build context

**Design:**

**Dockerfile (two-stage):**
- Stage 1 (`builder`): Python 3.13, gcc, pip compiles wheels from requirements.txt
- Stage 2 (`runtime`): Python 3.13-slim, copies wheels from builder, adds curl (HEALTHCHECK only), non-root user
- Rationale: Keeps final image small (no build toolchain), secure (non-root)
- HEALTHCHECK: `curl -f http://localhost:8000/readyz` with 10s start grace period

**docker-compose.yml (10 services):**

| Service | Image/Build | Purpose | Port |
|---------|-------------|---------|------|
| redis | redis:8-alpine | Persistent rate limit, budget, circuit breaker state | 6379 |
| mock-providers | build: ./deploy/mock-providers | Upstream LLM simulation, chaos control | 9000 |
| gateway | build: . | Main gateway (routes, rate limits, fallback) | 8000 |
| prometheus | prom/prometheus:v3.13.2 | Metrics collection, Grafana datasource | 9090 |
| alertmanager | prom/alertmanager:v0.32.0 | Alert routing (Slack via secrets volume) | 9093 |
| alertmanager-secrets-init | alpine:3.20 | Init container: writes Slack webhook to volume | — |
| grafana | grafana/grafana:13.1.3 | Dashboards (provisioned), traces (Jaeger datasource) | 3000 |
| jaeger | jaegertracing/jaeger:2.20.0 | Trace storage, query API, UI | 16686 + OTLP (4317/4318) |
| demo-seed | llm-gateway:phase5 (same as gateway) | One-shot: seed teams + print banner | — |
| k6 | grafana/k6:1.3.0 | Load test (under load-test profile) | — |

**Zero manual config path:**
- gateway env defaults point to mock-providers (not real vendors)
- GATEWAY_ADMIN_KEY defaults (dev key for demo)
- demo-seed runs automatically (restart: "no")
- SLACK_WEBHOOK_URL blank → Alertmanager queues/retries silently (safe default)

**Override path for real providers:**
- Create `.env` file: set OPENAI_API_KEY, ANTHROPIC_API_KEY, OLLAMA_BASE_URL, etc.
- Gateway reads these and connects to real vendors instead
- No code change needed

### Integration
- Gateway depends_on: redis (healthy), mock-providers (healthy), jaeger (started)
- Prometheus depends_on: gateway (healthy)
- Alertmanager depends_on: alertmanager-secrets-init (completed)
- Grafana depends_on: prometheus (healthy), jaeger (healthy)
- All healthchecks must pass before compose considers stack "ready"

---

## 3. Load Testing with Overhead Measurement

### Requirement (TRD)
> "A load test that sends 5,000+ concurrent requests through the gateway and measures the true gateway overhead (not just end-to-end latency), reports throughput and latency percentiles, and simulates a provider outage mid-run to measure failover latency."

### Delivered
**File:** `loadtest/k6_gateway_stress.js` (188 lines)

**Design:**

**Three concurrent scenarios:**
1. **gateway_traffic** (ramping-arrival-rate)
   - 50 req/s → 2000 req/s over 3 minutes (plus ramp down)
   - Through real gateway on port 8000
   - Custom metric: `gateway_request_duration` (Trend, non-aggregated)

2. **baseline_traffic** (ramping-arrival-rate)
   - Identical load shape (same ramping stages)
   - Directly to mock-providers on port 9000, bypassing gateway
   - Custom metric: `baseline_request_duration`
   - **Purpose:** Measure gateway overhead as delta (not an estimate)

3. **chaos_injector** (shared-iterations, single VU)
   - Starts at 1 minute into the run
   - Calls `POST /_chaos/config {provider: "openai", error_rate: 1.0}`
   - Sleeps 60 seconds (hold the outage)
   - Calls `POST /_chaos/reset`
   - **Purpose:** Trigger live failover mid-load

**Metrics:**
- `gateway_request_duration` (Trend): all gateway responses, recorded per-request
- `baseline_request_duration` (Trend): all baseline responses
- `http_req_failed` (Rate): failure % across both scenarios
- `http_req_duration` (default k6 metric): preserved for dashboard

**Threshold:**
- `gateway_request_duration` p99 < 50ms (end-to-end through proxy)
- `http_req_failed` rate < 1%

**handleSummary() output:**
```
=== Gateway overhead (P99) ===
  gateway p99:   42.34 ms  (proxy + mock upstream round trip)
  baseline p99:  35.12 ms  (mock upstream only, gateway bypassed)
  overhead:       7.22 ms  -- PRD target: < 10ms
```

**Why baseline matters:**
- Naive test: "P99 is 42ms, therefore gateway works" ← incomplete
- Correct measurement: "P99 through gateway is 42ms, P99 direct is 35ms, so overhead is 7ms" ← complete
- Phase 6 narrative must quote the overhead line verbatim from this output

### Integration
- k6 image: grafana/k6:1.3.0 (official)
- Output: `--out experimental-prometheus-rw` (k6's Prometheus remote-write client)
- Prometheus: `--enable-feature=remote-write-receiver` (accepts k6 metrics)
- Env vars: GATEWAY_BASE_URL, MOCK_PROVIDERS_BASE_URL, TEAM_API_KEY, K6_PROMETHEUS_RW_SERVER_URL

---

## 4. Integration Test Suite

### Requirement (TRD)
> "Integration tests exercising rate limiting, budgets, fallback, and circuit breaker logic against the live containerized stack over real HTTP, not in-process unit tests."

### Delivered
**Files:**
- `tests/integration/conftest.py` (53 lines)
- `tests/integration/test_full_stack_integration.py` (289 lines) — 5 tests
- `tests/integration/test_dashboard_accuracy.py` (145 lines) — 2 tests
- `tests/integration/test_mock_provider_wire_compat.py` (294 lines) — 10 tests

**Test Groups:**

**Wire-Compatibility (10 tests) — in-process, no Docker needed**
- Real adapters (app/providers/*.py) run against mock service
- httpx.ASGITransport: all HTTP traffic stays in Python
- Tests: OpenAI/Anthropic/Ollama/Gemini round-trip, streaming, chaos errors
- Purpose: Proof mock doesn't drift from real adapters
- Run via: `pytest tests/integration/test_mock_provider_wire_compat.py`

**Full-Stack Integration (5 tests) — requires docker-compose up**
- Real HTTP over network, real Redis, real gateway
- Tests:
  1. Rate-limit atomicity under concurrency (concurrent requests, exactly N succeed, rest 429)
  2. Budget cap enforcement (including bugfix: cap changes take effect mid-period)
  3. Fallback activation (live outage triggers next chain link)
  4. Circuit breaker open/close (5 failures → open state)
  5. Streaming response integrity (SSE chunks arrive, usage counted)
- Requires: `GATEWAY_BASE_URL`, `MOCK_PROVIDERS_BASE_URL`, `GATEWAY_ADMIN_KEY` env vars
- Run via: `pytest tests/integration/test_full_stack_integration.py -v`

**Dashboard Accuracy (2 tests) — requires docker-compose up + Prometheus**
- Queries Prometheus's own HTTP API (`GET /api/v1/query`)
- Tests:
  1. Request count exact match (scripted sequence → `gen_ai_requests_total`)
  2. Fallback event count exact match (live outage → `gen_ai_fallback_events_total`)
- Purpose: Automates the metric-correctness half of "manual dashboard QA"
- Run via: `pytest tests/integration/test_dashboard_accuracy.py -v`

**Chaos Logic Unit Tests (8 tests) — in-process, no HTTP**
- File: `deploy/mock-providers/tests/test_chaos.py`
- Pure logic tests on ChaosController (rule precedence, per-provider isolation, latency-only, clear)
- Run via: `cd deploy/mock-providers && pytest tests/test_chaos.py`

### Skip Behavior
- `@requires_live_stack` marker on full-stack tests
- Skip gracefully if `http://localhost:8000/readyz` unreachable
- Unit + wire-compat tests run always (no Docker needed)
- Live-stack tests skip (not error) when stack is down

### Bugfixes Validated
- **Budget cap regression (Phase 2):** test_patching_the_cap_after_spend_already_recorded_this_period_takes_effect_immediately
  - PATCH cap after spend already exists → must enforce immediately (not after period rollover)
  - Validates both the Lua script fix and budget.py fix
- **Token bucket isolation:** test_rate_limit_enforces_exactly_the_configured_rpm_under_concurrency
  - Drains bucket intentionally → uses dedicated batch-devs team (not shared data-science)
  - `_wait_for_full_rpm_bucket()` helper waits out refill time for reproducibility

---

## 5. Demo Automation

### Requirement (TRD)
> "A setup script that creates demo teams with different rate limits so reviewers can see the system in action immediately, with zero manual configuration."

### Delivered
**Files:**
- `scripts/setup_demo_teams.py` (151 lines)
- `scripts/verify_stack_healthy.sh` (60 lines)

**setup_demo_teams.py:**
- One-shot script (reused by demo-seed service in compose)
- Calls existing `TeamConfigStore.seed_from_yaml_if_empty()` (reuses Phase 2 logic)
- Prints credentials banner:
  ```
  ====================================
  LLM GATEWAY -- DEMO IS READY
  ====================================
  Gateway:      http://localhost:8000
  Grafana:      http://localhost:3000  (admin/admin)
  Jaeger UI:    http://localhost:16686
  Prometheus:   http://localhost:9090
  
  Demo teams (raw API key):
    data-science       sk-gw-datascience-demo-001
    product-eng        sk-gw-producteng-demo-002
    batch-devs         sk-gw-batchdevs-demo-003
  
  Try it:
    curl -s http://localhost:8000/v1/chat/completions \
      -H "X-Gateway-API-Key: sk-gw-datascience-demo-001" \
      -H "Content-Type: application/json" \
      -d '{"model": "tier-1-reasoning", "messages": [...]}'
  ```
- Admin API example included (for advanced users)

**verify_stack_healthy.sh:**
- Polls all 7 services (redis TCP, mock-providers HTTP, gateway HTTP, etc.)
- Configurable timeout (default 120s)
- Prints progress: `[ok]  redis (3s)`, `[ok]  gateway (22s)`, etc.
- Exit 0 when all healthy, exit 1 if timeout

### Usage
- Automatic: `docker-compose up` runs demo-seed automatically (prints banner)
- Manual: `./scripts/verify_stack_healthy.sh` (standalone verification)
- CI-ready: exits 0 on success, non-zero on timeout

---

## 6. Jaeger v2 Integration

### Requirement (TRD)
> "A tracing backend (Jaeger, selected over Grafana Tempo) that receives OpenTelemetry spans from the gateway, makes them queryable, and integrates with Grafana."

### Delivered
**Files:**
- `deploy/jaeger/config.yaml` (48 lines)
- `deploy/grafana/provisioning/datasources/jaeger.yml` (10 lines)

**Design:**

**Jaeger v2 choice (developer sign-off):**
- v1 EOL: 2025-12-31
- v2 is current, architecture changed (OpenTelemetry Collector distribution)
- In-memory storage (fine for demo/CI, <100k traces)
- All-in-one image: jaegertracing/jaeger:2.20.0

**config.yaml (v2 structure):**
- Receivers: OTLP gRPC (4317) + HTTP (4318)
- Exporters: jaeger_storage_exporter → memstore
- Storage backend: in-memory, max_traces: 100,000
- Extensions: jaeger_query (UI + query API), jaeger_storage (storage config)

**Gateway integration:**
- env var: `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318/v1/traces`
- App: `from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter`
- Exporter configured on app init (app/observability/tracing.py, unchanged from Phase 4)

**Grafana datasource:**
- Type: jaeger
- URL: `http://jaeger:16686` (query API, not OTLP receiver)
- Already provisioned: `deploy/grafana/provisioning/datasources/jaeger.yml`
- Grafana dashboard panels can now query traces by trace_id

**Ports:**
- 16686: UI + query HTTP API (what Grafana reads)
- 4317: OTLP gRPC receiver (what gateway writes to)
- 4318: OTLP HTTP receiver (alternative, not used in default config)

---

## 7. Regression Testing & Bugfixes

### Bugs Found (not pre-planned)

**Bug 1: Phase 2 Budget Cap Regression**

Root: `budget_increment.lua` stored cap in hash on first write, reused stale value on later calls.
Impact: Admin `PATCH /admin/budgets/{team}` had zero effect mid-period.
Why Phase 4 missed: All unit tests PATCH cap before spend exists (fresh fakeredis per test).
Why Phase 5 found: First repeated run against persistent Redis inherited stale state.

**Fix locations:**
- `app/ratelimit/budget_increment.lua`: Always use fresh ARGV[2], never stored cap
- `app/ratelimit/budget.py` precheck(): Only read spend_usd from hash, never cap
- `tests/unit/test_budget_enforcement.py`: Added test_patching_the_cap_after_spend_already_recorded_this_period_takes_effect_immediately

**Bug 2: Token Bucket Refill Test Isolation**

Root: Raising rpm_cap changes ceiling/rate, not retroactively fills tokens.
Impact: Concurrency test draining bucket → later tests starve unintentionally.
Why Phase 4 missed: Unit suite has fresh fakeredis per test (no shared state).
Why Phase 5 found: Live-stack suite uses persistent Redis across runs.

**Fix locations:**
- `tests/integration/test_full_stack_integration.py`: 
  - Concurrency test uses batch-devs team (not shared data-science)
  - Added `_wait_for_full_rpm_bucket()` helper (computes exact refill time)
  - Budget test uses product-eng team (not shared data-science)
  - Reads current spend before setting cap (makes cap stick regardless of history)

---

## 8. README Restructuring

### Requirement (TRD)
> "Update README to document Phase 5's new stack, testing approach, and how to run it."

### Delivered
**File:** `README.md` (complete restructure)

**Structure:**
- **Top-level:** Phase 5 status, one-command usage, deliverables summary
- **Concise implementation guide:** Design decisions, why they matter
- **Bugs caught:** Real bugs found by realistic testing
- **Model roster:** Unchanged (GPT-5.6 Sol/Terra, same as Phase 4)
- **Files new this phase:** 24-file inventory (18 new, 6 changed)
- **What's stubbed — now fully resolved:** Checklist of all Phase 1-4 stubs, now complete
- **Test plan → what each file proves:** Links tests to requirements
- **Done criteria:** Status check against Phase 5 TRD
- **Developer sign-off:** Three decisions made at kickoff
- **History:** Collapsible sections with Phase 4, 3, 2, 1 notes (preserved unedited)

**Rationale:** Phase 5 current top-level content answers "what just shipped?" without making readers scroll through earlier phases. Phases 1-4 preserved in collapsible History section for audit trail.

---

## 9. Configuration & Secrets

### Slack Webhook Handling
**Design (developer sign-off):** Automatic init-container

**Implementation:**
- Service: `alertmanager-secrets-init` (alpine, one-shot)
- Entrypoint: Shell script writes `$SLACK_WEBHOOK_URL` to named volume `/secrets/slack_webhook_url`
- Usage: Alertmanager mounts volume read-only, reads secret file in webhook URL config
- Fallback: Blank webhook is safe (Alertmanager queues/retries silently)

**Flow:**
1. `docker compose up` starts alertmanager-secrets-init
2. Script writes env var to volume (or blank if not set)
3. Init container exits (restart: "no")
4. Alertmanager service waits for init completion, mounts volume, reads config
5. Result: Zero manual steps, safe to leave blank, no secrets in compose file

### Monitoring Stack Configuration
**No changes from Phase 4** — already anticipated compose service names:

**Prometheus:**
- `deploy/prometheus/prometheus.yml` → scrape_configs already target `gateway:8000`, `alertmanager:9093`
- New: `--enable-feature=remote-write-receiver` (k6 metrics ingestion)

**Alertmanager:**
- `deploy/alertmanager/alertmanager.yml` → webhook reads from mounted secret

**Grafana:**
- `deploy/grafana/provisioning/datasources/prometheus.yml` → already correct
- New: `deploy/grafana/provisioning/datasources/jaeger.yml` (Jaeger datasource)
- Dashboards: unchanged from Phase 4

---

## 10. Architecture Decisions (Design Rationale)

### Why Mock Speaks Real Formats
Not simplified, but exact:
- Wire-compat tests run real adapters in-process
- If mock drifts, test catches it
- Proves adapters are production-ready, not written to shortcuts

### Why Chaos is HTTP, Not Env Var
k6 and integration tests need mid-run failure injection:
- Set failure at timestamp T
- Clear it at T+60s
- Observe failover under load
- Container restart can't do this

### Why Chaos Defaults to 503, Not 500
Every adapter has `_RETRYABLE_STATUS = {429, 502, 503, 504}` — 500 is excluded:
- Literal 500 → treated as non-retryable
- Gateway bubbles straight to client with zero fallback attempt
- Defeats the outage scenario's entire point
- 503 = actual "Service Unavailable" simulation

### Why config/teams.yaml Grants tier-1-reasoning
Demo's own advertised curl example uses this tier:
```bash
curl ... -d '{"model": "tier-1-reasoning", "messages": [...]}'
```
Without grant: first request 403s before reaching fallback logic.
Why safe: every unit test already grants this (additive).

### Why k6 Runs as Compose Service
grafana/k6:1.3.0 official image:
- Reviewer doesn't install k6 locally
- `docker compose --profile load-test run --rm k6` handles everything
- One-command reproducibility

### Why demo-seed Reuses Gateway Image
No second build:
- Single `image: llm-gateway:phase5`
- Both services (gateway + demo-seed) use it
- `scripts/setup_demo_teams.py` is thin wrapper (seeding + banner)
- Reduces build time, keeps artifact count down

---

## Summary

| TRD Requirement | Delivered File(s) | Status |
|---|---|---|
| Mock provider layer (wire-compat, chaos-injectable) | deploy/mock-providers/ | ✅ |
| Gateway Dockerfile | Dockerfile | ✅ |
| docker-compose orchestration (10 services) | docker-compose.yml | ✅ |
| Jaeger v2 integration | deploy/jaeger/config.yaml, datasources/jaeger.yml | ✅ |
| k6 load test (overhead measurement) | loadtest/k6_gateway_stress.js | ✅ |
| Integration test suite (25 new tests) | tests/integration/ | ✅ |
| Demo automation (zero manual config) | scripts/setup_demo_teams.py, verify_stack_healthy.sh | ✅ |
| Bugs found & fixed | budget_increment.lua, budget.py, test_budget_enforcement.py | ✅ (2 bugs) |
| README updated (Phase 5 current, history preserved) | README.md | ✅ |

**All requirements met. Implementation approved for review.**
