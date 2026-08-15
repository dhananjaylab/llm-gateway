# LLM Gateway — Phase 4: Observability Layer

Status: **built, tested, passing (193/193), zero flakiness across 5 consecutive runs**.
Timebox per the plan: Day 9-11. Phases 1-3 (unified proxy, rate
limiting/budgets, fallback/resilience) are complete and unchanged in
behavior this phase — see "History" below for their own notes, preserved
from when each shipped.

This is Phase 4 of 6 in the LLM Gateway project (see the project's own
Document 06, Implementation Plan). Phase 4's goal, verbatim from that doc:
> "every request, rejection, retry, and fallback is visible in a trace and
> a metric — an SRE should never need to read application logs to answer
> 'what just happened.'"

Phase 5 (integration/load testing + full docker-compose containerization)
and Phase 6 (portfolio polish) remain.

---

## Run it

```bash
python -m venv .venv && source .venv/bin/activate      # or .venv\Scripts\activate on Windows
pip install -r requirements.txt                          # includes pytest/respx — repo doesn't split runtime/dev deps
cp .env.example .env                                     # fill in whichever provider keys you have; Redis required (docker-compose up -d)
uvicorn app.main:app --reload --port 8000
```

Try it (needs a real `OPENAI_API_KEY`/`ANTHROPIC_API_KEY`, or just Ollama
running locally with `ollama pull llama3.2`):

```bash
curl -s http://localhost:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "X-Gateway-API-Key: sk-gw-datascience-demo-001" \
  -d '{"model": "ollama:llama3.2", "messages": [{"role":"user","content":"Say hi in 5 words."}]}'
```

Check the new observability surfaces:

```bash
curl -s http://localhost:8000/metrics | head -30                # Prometheus exposition
# Point OTEL_EXPORTER_OTLP_ENDPOINT at a local collector (e.g. the
# OpenTelemetry Collector's default OTLP/HTTP port) to see real traces —
# left unset by default; see "Concise implementation guide" below.
```

Run the test suite:

```bash
pytest -v
```

## Concise implementation guide (Phase 4)

- **Tracing setup lives in `app/observability/tracing.py`**, instantiated
  once per app (`app.state.tracer_provider`/`.tracer`), never via OTel's
  global `trace.set_tracer_provider()` — that API is a process-global
  singleton that only honors its *first* caller, which is exactly wrong
  for a test suite that builds a fresh FastAPI app per test. Every span
  call site reads `app.state.tracer` or receives it as a constructor arg
  (`FallbackRouter`), never `trace.get_tracer()`.
- **Wire `FastAPIInstrumentor` at `create_app()` time, not inside the
  lifespan.** Starlette lazily builds and *caches* its middleware stack
  on the very first ASGI call — and the lifespan's own "startup" scope
  IS that first call. Instrumenting from inside the lifespan (an earlier
  draft of this delivery did exactly that) means the middleware never
  makes it into the already-built stack, and the root SERVER span
  silently never gets created for real HTTP requests. Confirmed by hand
  against a running app before landing the fix.
- **CLIENT spans live in `app/resilience/fallback.py`, not
  `app/api/v1_chat.py`.** Document 05's worked trace example is one
  failed CLIENT span next to one succeeded CLIENT span, both children of
  the root — `FallbackRouter` already loops once per chain link, so
  that's the natural, minimal-diff place. This is also why
  `execute_non_streaming` now returns the already-translated
  `UnifiedChatResponse` instead of a provider's raw dict: usage numbers
  have to be known *before* a span closes to appear on it at all, and
  `adapter.translate_response(...)` is what produces them.
- **Metrics are scoped to a per-app `CollectorRegistry`
  (`app.state.metrics`), never `prometheus_client`'s global `REGISTRY`.**
  Same reasoning as tracing: a module-level `Counter`/`Histogram`/`Gauge`
  registered against the global registry raises "Duplicated timeseries"
  the moment a second test builds a second app in the same process.
- **Circuit-state gauge numbering is NOT the internal Lua state code.**
  Document 05 defines `gen_ai_circuit_breaker_state` as
  `0=Closed, 1=Half-Open, 2=Open`; `circuit_check.lua`/`circuit_record.lua`
  use `0=closed, 1=open, 2=half_open` internally — the 1/2 swap is real.
  `circuit_state_to_gauge_value()` in `app/observability/metrics.py` is
  the one place that conversion happens, always keyed off the STRING
  state, specifically so a future call site can't reintroduce the swap
  by reusing the Lua layer's raw int.
- **One metric beyond Document 05's literal 9:**
  `gen_ai_budget_utilization_ratio`. None of the original 9 make the
  TRD's own "team approaching budget cap" alert rule expressible in
  PromQL — `gen_ai_budget_applied_total` only increments on the *hard*
  402 block, and `gen_ai_cost_usd_total` has no per-team cap to divide
  against. The gauge is updated at the same point
  `app/ratelimit/budget.py` already computes spend/cap for the
  `X-Budget-Warning` response header — not new bookkeeping, just also
  exporting a number that already existed.
- **Classic (bucketed) Histograms, not native ones**, for the two
  latency metrics — Prometheus's own native histograms are stable
  server-side since v3.8, but `prometheus_client`'s authoring side only
  recently grew narrow, OpenMetrics-2.0-negotiation-only native-histogram
  exposition. Bucket boundaries are hand-tuned for this gateway's actual
  latency shape (sub-10ms overhead through multi-second generations), not
  the library's generic web-request defaults.
- **`gen_ai.*` schema is pinned to the pre-v1.42.0 baseline in code, not
  via `OTEL_SEMCONV_STABILITY_OPT_IN`.** As of this pass, `gen_ai.*`
  content lives in a dedicated, unversioned `semantic-conventions-genai`
  repo with nothing marked Stable — there's no schema version left to pin
  against the way the TRD originally asked. Opting into
  `gen_ai_latest_experimental` would trade the current baseline for a
  different, equally unstable one (and its content-capture event names
  were removed outright, not renamed — see the next point).
- **Opt-in prompt/completion capture uses a gateway-owned event name**
  (`gateway.content.capture`), deliberately not `gen_ai.*`-namespaced —
  that's precisely the part of the spec still being restructured
  upstream. Off by default (`OTEL_CAPTURE_MESSAGE_CONTENT=false`); when
  enabled, content goes out as a span EVENT only, never a span attribute,
  per the TRD's compliance constraint.
- **Grafana dashboards use classic file-based provisioning, not Git
  Sync.** Git Sync reached GA in Grafana 13.0 (current stable, not the
  TRD's assumed 12.x) but is built for a live instance synced against a
  GitHub/GitLab host — the wrong tool for a `docker-compose up` local
  demo with no external Git host in the loop.
- **`docker-compose.yml` stays Redis-only through Phase 4**, per explicit
  developer sign-off at Phase 4 kickoff. `deploy/prometheus/`,
  `deploy/alertmanager/`, and `deploy/grafana/` ship as real, tested-for-
  structure config now; the containers that actually mount and run them
  are Phase 5's "Containerize the full stack" deliverable.

## Model roster upgrade (developer sign-off, folded into this phase)

Per explicit sign-off at Phase 4 kickoff, `config/tiers.yaml`'s tier-1 and
tier-2 chains were upgraded from `gpt-5.4` to the GPT-5.6 "Sol/Terra"
family (GA July 9 2026, repriced July 30 2026):

- `tier-1-reasoning`: `openai:gpt-5.6-sol` replaces `openai:gpt-5.4` as
  primary.
- `tier-2-fast`: `openai:gpt-5.6-terra` is newly added as primary (this
  tier previously had no OpenAI link at all).
- `config/pricing.yaml` gained matching rows (Sol $5/$30, Terra $2/$12
  per MTok); `gpt-5.4`'s row is left in place, unused by any chain now
  but still resolvable for a literal request naming it directly.
- **Adapter fix this surfaced:** confirmed against current OpenAI/Azure
  OpenAI docs that GPT-5.6 and later models also reject `temperature`/
  `top_p`, not just `stop` — the same class of constraint
  `anthropic_adapter.py` already applies to Sonnet 5/Opus 4.7+.
  `openai_adapter.py` now omits all three unconditionally, same reasoning
  as the existing `stop` omission (every OpenAI model currently in scope
  is GPT-5.x).
- Health thresholds (down error rate 80%, degraded P99 latency 5000ms)
  were confirmed **kept as-is** per the same sign-off round — not TRD-
  specified originally, just Phase 3's own assumption, re-confirmed
  rather than silently carried forward unexamined.

## What's in this delivery (cumulative, all 4 phases)

```
app/
├─ main.py                      # FastAPI app factory; tracing wired at
│                                  create_app() time (see implementation
│                                  guide above); /healthz, /readyz, /metrics
├─ api/
│  ├─ v1_chat.py                 # POST /v1/chat/completions, GET /v1/models
│  └─ admin.py                   # /admin/limits, /budgets, /audit, /health, /circuits
├─ core/
│  ├─ schema.py                  # unified request/response Pydantic models
│  ├─ auth.py                    # X-Gateway-API-Key -> TeamConfig
│  ├─ config.py                  # settings: teams/tiers/pricing paths, Redis,
│  │                                rate limit, budget, circuit breaker, retry,
│  │                                health check, and Phase 4 observability
│  ├─ policy.py                  # system-prompt injection + regex PII redaction
│  ├─ team_store.py              # Redis-backed team config, hot-reload
│  ├─ audit.py                   # admin-action audit log (Redis Stream)
│  ├─ pricing.py                 # $ cost calculation from config/pricing.yaml
│  ├─ redis_client.py / redis_script.py
├─ providers/
│  ├─ base.py                    # ProviderAdapter interface + ProviderError
│  ├─ registry.py                 # "openai:gpt-5.6-sol" -> (adapter, "gpt-5.6-sol")
│  ├─ openai_adapter.py / anthropic_adapter.py / ollama_adapter.py / gemini_adapter.py
├─ ratelimit/
│  ├─ limiter.py                 # Redis Lua token bucket (RPM+TPM)
│  ├─ budget.py                  # $ budget precheck/record, warn+hard-cap
│  ├─ priority_queue.py          # batch-priority queueing
│  └─ estimator.py
├─ resilience/
│  ├─ circuit_breaker.py         # Redis-backed 3-state breaker + Prometheus gauge
│  ├─ fallback.py                # tier chain walk; CLIENT spans + fallback metric
│  ├─ retry.py                   # full-jitter exponential backoff
│  └─ health.py                  # active probes + passive rolling window
└─ observability/                # ** Phase 4 **
   ├─ tracing.py                  # TracerProvider, span helpers, content-capture
   └─ metrics.py                  # GatewayMetrics: the 9 Document-05 metrics + 1

config/
├─ teams.yaml                    # 3 demo teams — bootstrap seed for Redis
├─ tiers.yaml                    # tier -> fallback chain (GPT-5.6 roster, Phase 4)
└─ pricing.yaml                  # $ per-model rates

deploy/                          # ** Phase 4 ** — inert until Phase 5's containers
├─ prometheus/
│  ├─ alerts.yml                  # 4 TRD-named alert conditions
│  └─ prometheus.yml              # scrape config
├─ alertmanager/alertmanager.yml # Slack routing (webhook via mounted file, not env)
└─ grafana/
   ├─ provisioning/{datasources,dashboards}/*.yml
   └─ dashboards/{operations,business,performance}.json

scripts/hash_api_key.py, seed_teams.py
tests/unit/                      # 193 tests
```

## What's stubbed — now fully resolved

Every stub flagged in Phase 1's original delivery is now replaced:

| Stub | Replaced in |
| --- | --- |
| Rate limiting always allows | Phase 2 (Redis token bucket) — done |
| Circuit breaker always Closed | Phase 3 (Redis-backed state machine) — done |
| No retry / no fallback chain | Phase 3 — done |
| `teams.yaml` loaded once, no hot-reload | Phase 2 (Admin API + watcher) — done |
| No OTel spans / Prometheus metrics | **Phase 4 — done, this delivery** |
| Model id must be `provider:model` literal | Phase 3 (abstract tiers + fallback chains) — done |

Nothing is stubbed going into Phase 5. That phase's own scope (full
docker-compose containerization, k6 load test, integration suite against
the real network stack) is additive, not a replacement for anything above.

## Test plan → what each Phase 4 file proves

| Test file | Proves |
| --- | --- |
| `test_span_attributes.py` | Every `gen_ai.*` attribute in Document 05's schema is present and correctly typed on a successful CLIENT span; `error.type` on failure; the combined `auth.rate_limit_check` INTERNAL span wraps every request and reports ERROR on a 403; the root SERVER span carries `http.route`/`http.status_code`; scrape/probe endpoints produce zero spans |
| `test_trace_shape_on_fallback.py` | A forced-failover scenario reproduces Document 05's Journey B example almost exactly: one ERROR CLIENT span, one OK CLIENT span, both children of one root SERVER span whose own status is unaffected by the failed child — for both non-streaming and pre-first-chunk streaming failover |
| `test_metrics_exported.py` | Each of the 10 metrics (9 from Document 05 + `budget_utilization_ratio`) increments/updates correctly after a scripted request sequence; `GET /metrics` serves the app's own registry, not an empty one; the circuit-state gauge numbering matches Document 05, not the internal Lua code |
| `test_no_prompt_leakage.py` | With content capture disabled (the default), a distinctive marker string appears in *no* span attribute, span event, or Prometheus label anywhere — and, to prove the mechanism isn't just silently broken, that the same marker DOES appear once `OTEL_CAPTURE_MESSAGE_CONTENT=true` is set |
| `test_alert_rule_syntax.py` | `deploy/prometheus/alerts.yml` and `alertmanager.yml` parse, every rule has the fields Prometheus requires, every referenced `gen_ai_*` metric name is one this codebase actually declares (catches a typo/rename before it ever reaches a real Prometheus), and all 4 TRD-named alert conditions are present — plus the same metric-name cross-check applied to all 3 Grafana dashboard JSON files |

Regression: all 164 tests from Phases 1-3 pass unchanged in behavior
(some updated in-place for the GPT-5.6 model-roster upgrade — see below).

```
$ pytest -v
======================== 193 passed in ~8s ========================
```

Run 5 consecutive times during this delivery with zero flakiness, matching
the project's own acceptance bar from Phase 3 sign-off.

## Tests updated in place (not new, but changed by this phase)

- `tests/unit/test_fallback_chain.py`, `test_error_classification.py`,
  `test_admin_resilience.py`, `test_schema_normalization.py` — updated
  for the GPT-5.6 model-roster upgrade (tier-1's resolved chain now
  starts with `openai:gpt-5.6-sol`, not `gpt-5.4`; a new test locks in
  the temperature/top_p omission the upgrade surfaced).
- `tests/unit/conftest.py` — added `span_exporter`/`traced_app`/
  `traced_client` fixtures (an `InMemorySpanExporter` wired via
  `SimpleSpanProcessor` for synchronous, deterministic span assertions —
  `BatchSpanProcessor` doesn't flush in time for a test to inspect
  immediately after a request).

## Done criteria (from the project plan) — status

- [x] A single simulated incident (forced provider outage → fallback →
      recovery) is fully reconstructable from the trace/metric data alone:
      `test_trace_shape_on_fallback.py` proves the trace shape;
      `test_metrics_exported.py` proves `gen_ai_fallback_events_total` and
      `gen_ai_circuit_breaker_state` both reflect it. (Full Grafana-alone
      reconstruction needs Phase 5's actual Grafana container — the data
      this delivery exports is what makes that possible, not something
      this delivery can demonstrate standalone without a live dashboard.)
- [x] All three dashboards are valid JSON and reference only real,
      declared metric names — full "zero broken panels" QA against a live
      Grafana instance is Document 06's own explicit checklist item, not
      an automated test, and needs Phase 5's container.
- [ ] Alert rules firing in a live Alertmanager+Slack environment —
      structurally validated (`test_alert_rule_syntax.py`); an actual fire
      needs Phase 5's containers and a real or test Slack webhook.

## Developer sign-off requested before Phase 5

1. **OTLP collector target.** No collector is stood up this phase
   (`OTEL_EXPORTER_OTLP_ENDPOINT` ships blank) — point it at Jaeger/Tempo/
   your platform of choice once you have one, or confirm Phase 5's
   docker-compose should include a local collector alongside Prometheus/
   Grafana/Alertmanager.
2. **Slack webhook.** `deploy/alertmanager/alertmanager.yml` reads from a
   mounted secret file, not a checked-in value — confirm how Phase 5's
   compose wiring should populate it (init container, entrypoint script,
   or a documented manual step for anyone running the demo).
3. **`gen_ai_budget_utilization_ratio`** is a metric beyond Document 05's
   literal 9, added to make the TRD's own budget-alert requirement
   possible at all — confirm this is the right call rather than dropping
   that alert or redefining it against a metric that doesn't quite fit.

---

## History

The sections below are preserved from when each earlier phase shipped,
unedited except where explicitly noted, as the project's own audit trail.

<details>
<summary><strong>Phase 1 — original delivery notes</strong></summary>

Status: **built, tested, passing (40/40)**. Timebox per the plan: Day 1-3.

Phase 1's goal, verbatim from Document 06: "one internal request/response
shape, three provider adapters behind it, and a working streaming
passthrough — before any rate limiting or resilience logic exists."

### Design decisions made while building

1. **`max_completion_tokens` for OpenAI, not `max_tokens`.** Current-gen
   GPT-5.x chat models expect this field name.
2. **Ollama adapter targets the native `/api/chat` endpoint, not the
   `/v1/chat/completions` OpenAI-compat shim** — forces the adapter to
   handle a third distinct wire format (NDJSON), matching the TRD's
   normalization table.
3. **Streaming uses a hand-formatted `StreamingResponse`, not FastAPI's
   native `fastapi.sse.EventSourceResponse`** — that response class's
   encoding only activates for a route whose function is statically a
   generator, and this endpoint streams or not based on a field inside
   the request body, not knowable at route-registration time.
4. **Team API keys are hashed (`sha256:...`) in `teams.yaml` from Phase 1
   on**, not deferred to Phase 2.
5. **`requirements.txt` stays on classic `httpx`, not `httpx2`** —
   `respx`/`pytest-httpx` (needed for Phase 2/3's integration tests) are
   built against classic `httpx`.
6. **A fourth provider, Gemini, was added on top of the documented Phase 1
   scope.** `GeminiAdapter.stream()` deliberately raises a structured
   `ProviderError` rather than streaming — not implemented this phase.

### Patch note: Gemini adapter validation pass

Validating against a clean checkout surfaced three real bugs: (1)
`GeminiAdapter.stream()` had no `yield`, so Python treated it as a plain
coroutine — `async for` raised `TypeError` instead of the intended
`ProviderError`, and because `StreamingResponse` commits headers before
the body generator runs, a streaming Gemini request got a **silent, empty
200 OK**. Fixed with an unreachable `yield` after the `raise`. (2) A flaky
dotenv test that only passed if a real, untracked `.env` happened to
exist on the test machine — fixed by pointing at a throwaway `tmp_path`
`.env`. (3) `call()` leaked a stray `model` field into the outgoing
Gemini request body — fixed by stripping it from a payload copy before
the POST.

### Patch note: openai/anthropic live-call fixes validation pass

Manual testing against real accounts surfaced two adapter-level fixes:
Anthropic no longer sends `temperature`/`top_p` at all (Claude Sonnet
5/Opus 4.7+ return 400 on any non-default sampling parameter); OpenAI no
longer sends `stop` (GPT-5.4 rejects it with 400). Both are documented
directly in their respective adapters.

</details>

<details>
<summary><strong>Phase 2 note</strong></summary>

Rate limiting, budget enforcement, and the Admin API shipped in Phase 2 —
no README section was written for it at the time; see
`app/ratelimit/limiter.py`, `app/ratelimit/budget.py`, and
`app/api/admin.py`'s own module docstrings for the full design reasoning,
and `tests/unit/test_token_bucket_concurrency.py`,
`test_budget_enforcement.py`, `test_admin_api.py` for what was verified.

</details>

<details>
<summary><strong>Phase 3 note</strong></summary>

Tier-based fallback chains, retry with exponential backoff, Redis-backed
circuit breakers, and active/passive health checking shipped in Phase 3,
signed off at 163/163 passing with zero flakiness across many consecutive
runs — no README section was written for it at the time either; see
`app/resilience/fallback.py`, `circuit_breaker.py`, `retry.py`,
`health.py` module docstrings, and `tests/unit/test_fallback_chain.py`,
`test_circuit_breaker.py`, `test_retry_backoff.py`,
`test_health_checking.py` for what was verified.

</details>
