# Phase 5 — Reviewer Validation Checklist

Use this checklist to validate Phase 5 against a real Docker environment (the final validation step before Phase 6 kickoff).

---

## Pre-Validation Setup

### ✓ Environment Preparation
- [ ] Machine with Docker daemon + docker-compose v2.15+
- [ ] At least 6 GB RAM available (Prometheus, Jaeger, Grafana all heap-hungry)
- [ ] Ports 3000, 6379, 8000, 9000, 9090, 9093, 16686 available (or configure docker-compose.yml)
- [ ] `git clone https://github.com/dhananjaylab/llm-gateway.git && cd llm-gateway`
- [ ] On `main` branch (Phase 5 is committed here)

### ✓ Baseline Test (no Docker)
```bash
python -m pytest -q
# Expected: 211 passed, 7 skipped (~6s)
```
If FAIL: Phase 5 code isn't at your commit yet, or dependencies are missing.

---

## Docker Validation

### ✓ Image Builds
```bash
docker compose build
# Expected: 2 images built
#   - llm-gateway:phase5 (gateway + demo-seed)
#   - <repo>-mock-providers (mock service)
```
Check output for `Successfully built` and `Successfully tagged`.

### ✓ Cold Start (Health Check)
```bash
docker compose up -d
./scripts/verify_stack_healthy.sh
# Expected output:
#   [ok]  redis (Ns)
#   [ok]  mock-providers (Ns)
#   [ok]  gateway (Ns)
#   [ok]  prometheus (Ns)
#   [ok]  alertmanager (Ns)
#   [ok]  jaeger (Ns)
#   [ok]  grafana (Ns)
#   All services healthy after XXs.
# Expected: all services healthy in <90s
```
If TIMEOUT: check individual service logs (`docker compose logs <service>` to debug).

### ✓ Demo Credentials Banner
```bash
docker compose logs demo-seed
# Expected output should contain:
#   LLM GATEWAY -- DEMO IS READY
#   Gateway:      http://localhost:8000
#   Grafana:      http://localhost:3000  (admin/admin)
#   Jaeger UI:    http://localhost:16686
#   Prometheus:   http://localhost:9090
#   
#   Demo teams (raw API key):
#     data-science       sk-gw-datascience-demo-001
#     product-eng        sk-gw-producteng-demo-002
#     batch-devs         sk-gw-batchdevs-demo-003
#   
#   Try it:
#     curl -s http://localhost:8000/v1/chat/completions \
#       -H "X-Gateway-API-Key: sk-gw-datascience-demo-001" \
#       ...
```

---

## API Validation

### ✓ Gateway Readiness
```bash
curl -s http://localhost:8000/readyz | jq .
# Expected: {"status": "ready"}
```

### ✓ Mock-Providers Healthcheck
```bash
curl -s http://localhost:9000/healthz | jq .
# Expected: {"status": "ok"}
```

### ✓ Happy Path Request (No Chaos)
```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "X-Gateway-API-Key: sk-gw-datascience-demo-001" \
  -H "Content-Type: application/json" \
  -d '{"model": "tier-1-reasoning", "messages": [{"role": "user", "content": "hi"}]}' \
  | jq .
# Expected:
#   - status_code: 200
#   - choices[0].message.content: "This is a mock response..."
#   - usage.input_tokens: 10
#   - usage.output_tokens: 5
#   - X-Gateway-Served-Model: "openai:gpt-5.4" (or another real provider)
```

### ✓ Simulate Outage & Failover
```bash
# Inject chaos (force openai to fail)
curl -s -X POST http://localhost:9000/_chaos/config \
  -H "Content-Type: application/json" \
  -d '{"provider": "openai", "error_rate": 1.0}'

# Send the same request again (should failover to anthropic)
curl -s http://localhost:8000/v1/chat/completions \
  -H "X-Gateway-API-Key: sk-gw-datascience-demo-001" \
  -H "Content-Type: application/json" \
  -d '{"model": "tier-1-reasoning", "messages": [{"role": "user", "content": "hi"}]}' \
  | jq .

# Expected:
#   - status_code: 200 (NOT 503, NOT 500 — fallback worked)
#   - X-Gateway-Served-Model: "anthropic:claude-sonnet-5" (fallback chain link)
#   - choices[0].message.content: still got a response

# Clear the chaos
curl -s -X POST http://localhost:9000/_chaos/reset
```

### ✓ Rate Limiting Under Load
```bash
# Send 50 concurrent requests to a team with rpm_cap=10
for i in {1..50}; do
  curl -s http://localhost:8000/v1/chat/completions \
    -H "X-Gateway-API-Key: sk-gw-batchdevs-demo-003" \
    -H "Content-Type: application/json" \
    -d '{"model": "ollama:llama3.2", "messages": [{"role": "user", "content": "hi"}]}' &
done
wait

# Expected: ~10 succeed (200), ~40 are throttled (429)
```

---

## Metrics & Observability

### ✓ Prometheus Metrics Export
```bash
curl -s http://localhost:8000/metrics | grep -E "^gen_ai_" | head -10
# Expected: metrics like:
#   gen_ai_requests_total{...} X
#   gen_ai_request_duration_seconds{...} Y
#   gen_ai_fallback_events_total{...} Z
#   gen_ai_circuit_breaker_state{...}
```

### ✓ Prometheus API Query
```bash
curl -s "http://localhost:9090/api/v1/query?query=gen_ai_requests_total" | jq .
# Expected:
#   status: "success"
#   data.result: array of timeseries
```

### ✓ Jaeger Trace Export
```bash
# Make a request
curl -s http://localhost:8000/v1/chat/completions \
  -H "X-Gateway-API-Key: sk-gw-datascience-demo-001" \
  -H "Content-Type: application/json" \
  -d '{"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]}'

# Check Jaeger has spans
curl -s "http://localhost:16686/api/traces?service=gateway" | jq .
# Expected:
#   data: array of trace objects
#   Each trace contains spans for request → translate → call → fallback etc.
```

---

## Dashboard Validation

### ✓ Grafana Access
```bash
# Navigate to http://localhost:3000
# Login: admin / admin
```

### ✓ Datasources
Confirm both datasources are provisioned and healthy:
- [ ] Prometheus: Settings → Data Sources → Prometheus → Test
  - Expected: green checkmark "Data source is working"
- [ ] Jaeger: Settings → Data Sources → Jaeger → Test
  - Expected: green checkmark "Data source is working"

### ✓ Operations Dashboard
Navigate to Dashboards → Operations:
- [ ] Circuit Breaker State panel: shows state per provider:model
- [ ] Fallback Events panel: empty initially, shows spike during chaos test
- [ ] Request Rate panel: shows request throughput
- [ ] No "No Data" errors in any panel

### ✓ Business Dashboard
Navigate to Dashboards → Business:
- [ ] Spend by Team panel: shows spend for data-science, product-eng, batch-devs
- [ ] Budget Utilization panel: shows ratio of spend/cap
- [ ] No "No Data" errors

### ✓ Performance Dashboard
Navigate to Dashboards → Performance:
- [ ] P99 Latency panel: shows gateway latency
- [ ] No "No Data" errors

---

## Load Test Execution

### ✓ Run k6 Load Test
```bash
docker compose --profile load-test run --rm k6
# Expected runtime: ~4-5 minutes
# Expected output at end:
#
# === Gateway overhead (P99) ===
#   gateway p99:   XX.XX ms  (proxy + mock upstream round trip)
#   baseline p99:  YY.YY ms  (mock upstream only, gateway bypassed)
#   overhead:      Z.ZZ ms  -- PRD target: < 10ms
```

### ✓ Capture Load Test Metrics
Copy the final output section verbatim:
```
overhead:      Z.ZZ ms  -- PRD target: < 10ms
```

**This exact number must appear in the Phase 6 narrative** (see Phase 6 prerequisites).

### ✓ Verify Grafana During Load Test
Open Grafana (http://localhost:3000) in a second terminal while k6 runs:
- [ ] Operations dashboard: Request Rate panel shows 50+ req/s
- [ ] Circuit Breaker State: no circuits open (unless chaos scenario is active)
- [ ] At T~1min into k6: Fallback Events panel shows spike (chaos injector is active)
- [ ] At T~2min: Fallback Events spike subsides (chaos cleared)

---

## Alertmanager Validation

### ✓ Alertmanager Healthy
```bash
curl -s http://localhost:9093/-/healthy
# Expected: 200 OK
```

### ✓ Check Alert Rules Loaded
```bash
curl -s http://localhost:9090/api/v1/rules | jq '.data.groups[].rules[] | {alert: .alert, state: .state}'
# Expected: array of alert rules (gen_ai_rate_limit_breached, etc.)
```

### ✓ Trigger a Test Alert (Optional, requires Slack webhook)
If SLACK_WEBHOOK_URL is set:
```bash
# Manually trigger an alert via Alertmanager
curl -s -X POST http://localhost:9093/api/v1/alerts \
  -H "Content-Type: application/json" \
  -d '[{"labels": {"alertname": "TestAlert"}, "annotations": {"summary": "Test"}}]'

# Check Slack: should receive a message
```

---

## Live-Stack Integration Tests

### ✓ Run Integration Tests (Stack Must Be Up)
```bash
GATEWAY_BASE_URL=http://localhost:8000 \
MOCK_PROVIDERS_BASE_URL=http://localhost:9000 \
GATEWAY_ADMIN_KEY=dev-admin-key-change-me \
  pytest tests/integration/ -v
# Expected: 5 full-stack + 2 dashboard accuracy tests PASS
```

If SKIP: stack not detected (check readyz endpoint).  
If FAIL: debug with `docker compose logs <service>`.

---

## Cleanup & Verification

### ✓ Teardown
```bash
docker compose down -v
# Expected: all containers stopped, volumes removed
```

### ✓ Confirm No Dangling State
```bash
docker compose ps
# Expected: no services running
```

### ✓ Re-Run Unit Tests (Post-Docker)
```bash
pytest -q
# Expected: 211 passed, 7 skipped
# (same as baseline, Docker didn't break anything)
```

---

## Sign-Off

### ✓ All Checkboxes Complete?

If **ALL** checkboxes above are checked:
- ✅ Phase 5 implementation is **validated and ready for Phase 6**
- ✅ Load test results are **captured** (overhead number)
- ✅ Grafana dashboards are **operational**
- ✅ Alertmanager is **configured**

### ✓ Phase 6 Prerequisites Satisfied?

1. **Load test numbers captured:** Yes ✅
   - Overhead: [paste P99 delta here]
   - Throughput: [note any observations]
   - Failover latency: [note any observations]

2. **Grafana dashboard QA passed:** Yes ✅
   - All panels resolve
   - Circuit breaker timeline shows state changes
   - Spend/budget panels accurate

3. **Real-provider demo decision made:** Yes ✅
   - [ ] Mock-only (default, recommended)
   - [ ] Real Ollama segment (optional)
   - [ ] Real OpenAI segment (optional)

---

## Document Reference

| Document | Use For |
|---|---|
| PHASE5_SIGNOFF.md | Formal approval + architecture review |
| PHASE5_EXECUTIVE_SUMMARY.md | Quick reference (1-page overview) |
| PHASE5_IMPLEMENTATION_GUIDE.md | How TRD maps to delivered code |
| This checklist | Step-by-step validation (this file) |
| llm-gateway-phase5.zip | Delivery artifact (24 files) |

---

**Date Validated:** ______________________  
**Validated By:** ______________________  
**Status:** ✅ **APPROVED FOR PHASE 6 KICKOFF**
