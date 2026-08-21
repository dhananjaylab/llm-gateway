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

## Option 2: Local Development (Without Docker)

**Best for:** Faster iteration, running just the gateway locally

### Setup

#### 1. Start Jaeger locally
```bash
# If you have Docker but don't want full compose:
docker run -d \
  --name jaeger-local \
  -p 16686:16686 \
  -p 4318:4318 \
  -v C:\llm-gateway\deploy\jaeger\config.yaml:/etc/jaeger/config.yaml:ro \
  jaegertracing/jaeger:2.20.0 \
  --config /etc/jaeger/config.yaml
```

Or download [Jaeger binary](https://www.jaegertracing.io/download/) for Windows:
```bash
jaeger --config=deploy/jaeger/config.yaml
```

#### 2. Start Redis
```bash
# If using local Redis
docker run -d -p 6379:6379 redis:8-alpine
# OR use your RedisLabs URL (already in .env)
```

#### 3. Set tracing endpoint in .env
```env
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318/v1/traces
```

#### 4. Start the gateway
```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### What to expect:
- Spans exported to Jaeger on every request
- View traces at http://localhost:16686
- No metrics/dashboards (Prometheus separate if needed)

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
| **Docker Compose** | ✓ | ✓ | ✓ | 30s | Free (mock only) | Full demo, review |
| **Local Dev** | ✓ | ✗ | ✗ | 2-3 min | Free | Tracing focused |
| **Collectors Only** | ✗ | ✓ | ✓ | 15s | Free | Metrics focused |
| **None** | ✗ | ✗ | ✗ | 5s | Free | Quick testing |

---

## Testing the Setup

### Option 1/2 (with Jaeger):
```bash
curl -X POST http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Gateway-API-Key: sk-gw-datascience-demo-001" \
  -d '{
    "model": "openai:gpt-5.6-sol",
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

1. **Pick your option** based on use case above
2. **Start the services** (docker compose or individual tools)
3. **Update `.env`** with the `OTEL_EXPORTER_OTLP_ENDPOINT` 
4. **Restart the gateway** (if already running)
5. **Make a request** and check Jaeger at `http://localhost:16686`

For full details, see:
- `README.md` "Run it" section (docker-compose full walkthrough)
- `deploy/jaeger/config.yaml` (Jaeger v2 configuration)
- `app/observability/tracing.py` (span creation logic)
