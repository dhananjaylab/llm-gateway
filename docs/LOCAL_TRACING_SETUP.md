# Local Tracing Setup Options

The warning you're seeing means spans are being created but have nowhere to go. Here are your local options:

## Option 1: Docker Compose Stack (Recommended for Full Demo)

**Best for:** See the full observability stack with Jaeger UI, Prometheus, and Grafana

### Setup
```bash
docker compose up -d
./scripts/verify_stack_healthy.sh
docker compose logs -f demo-seed
```

This gives you:
- **Jaeger UI**: http://localhost:16686 (traces)
- **Prometheus**: http://localhost:9090 (metrics)
- **Grafana**: http://localhost:3000 (dashboards, admin/admin)
- **Gateway**: http://localhost:8000
- **Mock Providers**: http://localhost:9000 (zero-cost test doubles)

The compose file already sets `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318/v1/traces` for the gateway container.

### Why this works:
- **Jaeger v2** runs all-in-one with in-memory storage (`deploy/jaeger/config.yaml`)
- OTLP/HTTP receiver on port 4318 (what your gateway uses)
- Zero real API keys needed (mock-providers is a wire-compatible test double)
- Prometheus scrapes gateway metrics automatically
- Grafana has Jaeger datasource pre-configured

---

## Option 2: Local Development (Recommended for Iteration)

**Best for:** Faster development iteration with full observability stack (Jaeger + Prometheus + Grafana)

### Setup — Start Backend Services

#### 1. Start Jaeger (traces)
```bash
docker run -d \
  --name jaeger-local \
  -p 16686:16686 \
  -p 4318:4318 \
  -v C:\llm-gateway\deploy\jaeger\config.yaml:/etc/jaeger/config.yaml:ro \
  jaegertracing/jaeger:2.20.0 \
  --config /etc/jaeger/config.yaml
```

Verify: http://localhost:16686 (you should see the Jaeger UI)

#### 2. Start Prometheus (metrics)
```bash
docker run -d \
  --name prometheus-local \
  -p 9090:9090 \
  -v C:\llm-gateway\deploy\prometheus\prometheus.yml:/etc/prometheus/prometheus.yml:ro \
  -v C:\llm-gateway\deploy\prometheus\alerts.yml:/etc/prometheus/alerts.yml:ro \
  prom/prometheus:v3.13.2 \
  --config.file=/etc/prometheus/prometheus.yml \
  --web.enable-lifecycle
```

Verify: http://localhost:9090 (Prometheus should be scraping `localhost:8000/metrics`)

#### 3. Start Grafana (dashboards)
```bash
docker run -d \
  --name grafana-local \
  -p 3000:3000 \
  -e GF_SECURITY_ADMIN_USER=admin \
  -e GF_SECURITY_ADMIN_PASSWORD=admin \
  -v C:\llm-gateway\deploy\grafana\provisioning:/etc/grafana/provisioning:ro \
  -v C:\llm-gateway\deploy\grafana\dashboards:/var/lib/grafana/dashboards:ro \
  grafana/grafana:13.1.3
```

Verify: http://localhost:3000 (login with admin/admin)

#### 4. Start Redis
```bash
# Option A: Local container
docker run -d \
  --name redis-local \
  -p 6379:6379 \
  redis:8-alpine

# Option B: Use your RedisLabs URL (already in .env)
# Just leave REDIS_URL as-is, no action needed
```

### Setup — Update .env and Start Gateway

#### 5. Update `.env` for local backends
```env
# Tracing endpoint
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces

# If using local Redis (Option A above):
REDIS_URL=redis://localhost:6379/0

# If using RedisLabs (Option B):
# Leave REDIS_URL unchanged (already in .env)
```

#### 6. Start the gateway
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### What to expect:
- **Jaeger UI**: http://localhost:16686 → Traces appear on every request
- **Prometheus**: http://localhost:9090 → Metrics at `gateway:8000:8000`
- **Grafana**: http://localhost:3000 → Pre-provisioned dashboards (operations, business, performance)
- **Gateway**: http://localhost:8000 → Your app, hot-reload enabled

### Cleanup (stop all local services)
```bash
docker stop jaeger-local prometheus-local grafana-local redis-local
docker rm jaeger-local prometheus-local grafana-local redis-local
```

---

## Option 3: Just the Collectors (Prometheus + Alertmanager + Grafana)

**Best for:** Metrics and dashboards without tracing

```bash
# From docker-compose.yml, pick specific services:
docker compose up -d prometheus alertmanager grafana redis

# Keep tracing endpoint unset (or set to /dev/null equivalent)
OTEL_EXPORTER_OTLP_ENDPOINT=
```

Then:
```bash
uvicorn app.main:app --reload --port 8000
```

Access:
- **Prometheus**: http://localhost:9090
- **Grafana**: http://localhost:3000 (admin/admin)
- **Metrics**: http://localhost:8000/metrics

---

## Option 4: No Observability Backend (Quickest Dev)

**Best for:** Pure functional testing, ignoring the warning

```bash
# Leave .env as-is:
OTEL_EXPORTER_OTLP_ENDPOINT=

# Start gateway
uvicorn app.main:app --reload --port 8000
```

**Trade-off:** Spans are created in-process (usable by test harnesses like `InMemorySpanExporter`) but never exported. Warning still appears.

---

## Quick Comparison

| Option | Jaeger | Prometheus | Grafana | Setup Time | Cost | Use Case |
|--------|--------|------------|---------|-----------|------|----------|
| **Local Dev (Option 2)** | ✓ | ✓ | ✓ | 5-10 min | Free | **Recommended** — full stack, hot-reload |
| **Docker Compose** | ✓ | ✓ | ✓ | 30s | Free (mock only) | Demo, review, zero config |
| **Collectors Only** | ✗ | ✓ | ✓ | 15s | Free | Metrics focused |
| **None** | ✗ | ✗ | ✗ | 5s | Free | Quick testing |

---

## Testing the Setup

### Option 2 (Local Dev with full stack):
```bash
# Make a request to the gateway
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Gateway-API-Key: sk-gw-datascience-demo-001" \
  -d '{
    "model": "openai:gpt-5.6-sol",
    "messages": [{"role": "user", "content": "Say hi"}]
  }'

# Check results:
# - Jaeger traces: http://localhost:16686 (search for service "llm-gateway")
# - Prometheus metrics: http://localhost:9090 (query: gen_ai_requests_total)
# - Grafana dashboards: http://localhost:3000 (operations/business/performance tabs)
```

### Option 1 (Docker Compose):
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Gateway-API-Key: sk-gw-datascience-demo-001" \
  -d '{
    "model": "tier-1-reasoning",
    "messages": [{"role": "user", "content": "Say hi"}]
  }'

# Then check: http://localhost:16686
# Search for service "llm-gateway"
```

### Option 3 (with metrics):
```bash
# Make the same request above, then:
curl http://localhost:8000/metrics | grep gen_ai
```

---

## Architecture Overview

```
Your Gateway App
    ↓
OTLPSpanExporter (opentelemetry-exporter-otlp-proto-http)
    ↓
OTEL_EXPORTER_OTLP_ENDPOINT (HTTP)
    ↓
┌─────────────────────────────────────────────┐
│  Jaeger v2 (OTLP/HTTP Receiver port 4318)   │
│  ┌─────────────────────────────────────────┐│
│  │  In-Memory Storage (100k traces max)    ││
│  │  Auto-evicts oldest when full           ││
│  └─────────────────────────────────────────┘│
│  ┌─────────────────────────────────────────┐│
│  │  Jaeger Query (port 16686 UI)           ││
│  │  Grafana Datasource (query API)         ││
│  └─────────────────────────────────────────┘│
└─────────────────────────────────────────────┘
```

---

## Troubleshooting

**Warning appears but nothing breaks?**
- Completely normal in local dev
- Spans are still created (used by unit tests via `InMemorySpanExporter`)
- Just never exported to a backend

**Can't connect to `localhost:4318`?**
- Make sure Jaeger is running: `docker ps | grep jaeger` or check `http://localhost:16686`
- Update `.env`: ensure `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces`
- Restart the gateway

**Traces appear in Jaeger but they're empty?**
- Check if spans have attributes: expand the span details
- If completely empty, check `app/observability/tracing.py` — spans are created with specific attributes per `provider_call_span()` and `set_span_success()`

**Docker compose won't start?**
- Missing image: run `docker compose pull` first
- Port conflict: check `docker ps` and kill conflicts or change compose ports
- For detailed help: `docker compose logs -f`

---

## Next Steps

### For Option 2 (Recommended):
1. Copy the 4 `docker run` commands for Jaeger, Prometheus, Grafana, Redis
2. Run them in sequence (copy-paste into terminal)
3. Update `.env` with `OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces`
4. Start the gateway: `uvicorn app.main:app --reload --port 8000`
5. Make a test request (curl example above)
6. View results across all three observability tools

### For Option 1 (Docker Compose):
1. Run `docker compose up -d`
2. Run `./scripts/verify_stack_healthy.sh`
3. Check `docker compose logs -f demo-seed` for demo team keys
4. Make a test request

### Stopping all services:
```bash
# Option 2 local containers
docker stop jaeger-local prometheus-local grafana-local redis-local
docker rm jaeger-local prometheus-local grafana-local redis-local

# Option 1 compose stack
docker compose down
```

For full details, see:
- `README.md` "Run it" section (docker-compose full walkthrough)
- `deploy/jaeger/config.yaml` (Jaeger v2 configuration)
- `deploy/prometheus/prometheus.yml` (metric scraping config)
- `deploy/grafana/provisioning/` (dashboard provisioning)
- `app/observability/tracing.py` (span creation logic)
