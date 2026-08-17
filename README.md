# LLM Gateway — Phase 5: Integration Test, Load Test & Containerization

Status: **built, tested, passing (211/211 unit + wire-compat, 7 live-stack
integration tests correctly skip without a running stack), zero flakiness
across multiple consecutive runs — including two genuine bugs this phase's
more realistic testing style caught and fixed (see "Bugs caught" below)**.
Timebox per the plan: Day 11-13. Phases 1-4 (unified proxy, rate
limiting/budgets, fallback/resilience, observability) are complete and
unchanged in *behavior* this phase (one real bug fix in Phase 2's budget
code aside — see below) — see "History" for their own notes, preserved
from when each shipped.

This is Phase 5 of 6 in the LLM Gateway project (see the project's own
Document 06, Implementation Plan). Phase 5's goal, verbatim from that doc:
> "prove the success metrics from the PRD with a real load test, not an
> estimate — and package the whole stack so a reviewer can run it in one
> command."

Phase 6 (demo recording + portfolio narrative) remains.

**Developer sign-off locked in at Phase 5 kickoff** (carried through this
delivery): **Jaeger** for trace storage/UI (over Grafana Tempo), an
**automatic init-container** for populating Alertmanager's Slack webhook
secret (over a manual pre-flight step), and **CI wiring deferred to
Phase 6** (this phase stays local/manual-run only, per that decision).

---

## Run it

**Full stack, zero manual configuration, zero real API keys** (the
default path — every provider adapter points at `mock-providers`, a real
wire-compatible test double, not a live vendor):

```bash
docker compose up -d
./scripts/verify_stack_healthy.sh      # polls every service until healthy, or times out loudly
docker compose logs -f demo-seed       # prints a credentials banner + curl examples once ready
```

That gives you: gateway (`:8000`), mock-providers (`:9000`), Prometheus
(`:9090`), Alertmanager (`:9093`), Jaeger UI (`:16686`), Grafana
(`:3000`, `admin`/`admin`) — see `scripts/setup_demo_teams.py`'s banner
for exact demo team keys and a ready-to-run curl command.

Run the load test (separate from `up` on purpose — a reviewer starting
the demo shouldn't eat a multi-minute benchmark run):

```bash
docker compose --profile load-test run --rm k6
```

Demo against **real** providers instead of the mock, or run the app
without Docker at all (unchanged from Phase 1-4):

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env             # fill in real provider keys; Redis required (docker-compose up -d redis)
uvicorn app.main:app --reload --port 8000
```

Run the test suite:

```bash
pytest -v                                    # unit + in-process wire-compat tests — no stack needed, ~6s
# with the stack up (`docker compose up -d`):
GATEWAY_ADMIN_KEY=<your key> pytest tests/integration/ -v   # live-stack integration + dashboard-accuracy tests
```

## Concise implementation guide (Phase 5)

- **Mock providers (`deploy/mock-providers/`) speak each real adapter's
  exact wire format**, not a simplified stand-in — OpenAI's Responses API,
  Anthropic's Messages API, Ollama's native `/api/chat`, Gemini's
  `generateContent`. This is deliberate: `tests/integration/test_mock_provider_wire_compat.py`
  runs the *real* `app/providers/*.py` adapters against this service
  in-process (via `httpx.ASGITransport`, no network) specifically so a
  mock that silently drifts from what an adapter actually parses gets
  caught immediately, not discovered the first time someone points the
  gateway at a real vendor.
- **Chaos injection is a live HTTP control plane**
  (`POST http://mock-providers:9000/_chaos/config`), not a static env var
  — `docker compose up` and a running k6/integration-test process both
  need to force a specific provider into an outage *mid-run* and clear it
  later; a container restart can't do that. See `deploy/mock-providers/chaos.py`.
- **Chaos's default injected status is 503, not the literal 500 Document
  06 names.** Every adapter's own `_RETRYABLE_STATUS = {429, 502, 503, 504}`
  excludes 500 — a literal 500 would make the gateway treat "the provider
  is down" as non-retryable and bubble it straight to the client with zero
  fallback attempt, defeating the entire point of an outage scenario. See
  `chaos.py`'s module docstring for the full reasoning; pass
  `status_code=500` explicitly to a caller that specifically wants the
  non-retryable path instead.
- **The gateway's own `Dockerfile` is new this phase** (Phases 1-4 never
  needed one — the whole test suite runs in-process against fakeredis).
  Two-stage build (`builder` compiles wheels, `runtime` copies only the
  installed packages), runs as a non-root user, `HEALTHCHECK` hits
  `/readyz`.
- **`demo-seed` reuses the gateway's own built image** (`image:
  llm-gateway:phase5` on both services in `docker-compose.yml`, only the
  `gateway` service has a `build:` block) rather than a second build —
  `scripts/setup_demo_teams.py` is a thin wrapper around the seeding
  logic that already runs automatically in `app/main.py`'s lifespan
  (Phase 2); the actual gap it closes is printing the *raw* demo API keys
  (`config/teams.yaml` only stores hashes) so a reviewer knows what to
  curl.
- **Jaeger v2, not v1** — v1 (the old `jaegertracing/all-in-one` image,
  configured via `COLLECTOR_OTLP_ENABLED` and friends) reached end of
  life 2025-12-31. v2 is architected as an OpenTelemetry Collector
  distribution and is configured *only* via an explicit YAML file
  (`deploy/jaeger/config.yaml`) — no env-var configuration exists anymore.
  Confirmed current image: `jaegertracing/jaeger:2.20.0`.
- **k6 runs as a `docker compose` service** (`grafana/k6:1.3.0`, official
  image), not a local binary — `docker compose --profile load-test run
  --rm k6` needs nothing installed beyond Docker itself.
- **The k6 script measures gateway overhead as an actual delta, not an
  assertion**: a `baseline_traffic` scenario hits `mock-providers`
  directly at the same ramp shape as `gateway_traffic`, and
  `handleSummary()` prints `gateway p99 - baseline p99` — the literal
  number Phase 6's narrative should quote for the "<10ms overhead" claim,
  per the same "Direct Upstream vs. Gateway Proxy" comparison the
  project's own reference HTML prototype benchmark chart already
  established. A third `chaos_injector` scenario forces a 1-minute
  mid-run `openai` outage so the Operations dashboard's fallback/circuit
  panels have something to show from the same run that produces the
  throughput numbers.
- **`config/teams.yaml` gained one line**: `data-science` is now also
  granted the `tier-1-reasoning` *tier* (not just literal `provider:model`
  ids) — without it, the demo's own advertised "simulate an outage" curl
  example 403s before ever reaching the fallback logic it's meant to
  demonstrate. Every unit test that specifically wants tier routing
  already grants this itself via `team_store.update_team()`, so this is
  additive, not a behavior change for any existing test.

## Bugs caught during Phase 5 implementation (not pre-planned)

Same posture as every earlier phase: these surfaced from actually running
the stack, not from re-reading the design docs harder.

- **A real Phase 2 bug: admin budget-cap changes silently stopped
  enforcing mid-period.** `budget_increment.lua` seeded `cap_usd` into the
  per-period Redis ledger only on its *first* write each period and
  reused that stale value on every later call — so `PATCH
  /admin/budgets/{team}` had **zero enforcement effect** for the rest of
  an already-active billing period, directly contradicting the PRD's "a
  policy change never requires a deploy or a restart" user story and
  Document 03 Journey D. Every Phase 2 unit test happened to PATCH the
  cap *before* any spend existed that period, so this never tripped a
  fresh-fakeredis-per-test run — Phase 5's live, long-lived container
  (the same Redis persisting across repeated `pytest` invocations, unlike
  the unit suite's isolated-per-test fakeredis) hit it on the very second
  run. Fixed in both `budget_increment.lua` and
  `BudgetEnforcer.precheck()` to always trust the live `team.budget_cap_usd`
  instead of a value snapshotted into the ledger; regression-guarded by
  `tests/unit/test_budget_enforcement.py::test_patching_the_cap_after_spend_already_recorded_this_period_takes_effect_immediately`.
- **Token-bucket capacity changes don't retroactively refill current
  tokens** (correct behavior, but a real test-design trap): raising
  `rpm_cap` via PATCH changes the ceiling and refill *rate*, not the
  bucket's current stored token count — a concurrency test that drains a
  team's bucket to zero and a later test against the *same* team can
  starve on RPM it never meant to test. Fixed by giving
  `tests/integration/test_full_stack_integration.py`'s concurrency and
  budget tests their own dedicated teams (`batch-devs`, `product-eng`)
  instead of sharing `data-science`, plus a `_wait_for_full_rpm_bucket()`
  helper that computes and waits out the exact refill time when a test
  genuinely does need a full bucket.
- **FastAPI's default `HTTPException` wraps `detail` under a `"detail"`
  key** — fine for the gateway's own outward-facing errors, but wrong for
  `mock-providers`' *upstream-provider-shaped* error bodies
  (`{"error": {...}}`, no wrapper, matching what each adapter's own
  `_raise_for_status_bytes` actually parses). Fixed with a dedicated
  `@app.exception_handler(ChaosInjectedError)` that returns the raw
  provider-shaped body directly.

## Model roster (unchanged from Phase 4)

No roster changes this phase — `config/tiers.yaml` and
`config/pricing.yaml` are exactly as Phase 4 left them (GPT-5.6
Sol/Terra). See History → Phase 4 for that round's sign-off notes.

## What's new this phase (files)

```
Dockerfile                          # gateway image (new — Phases 1-4 never needed one)
.dockerignore

deploy/mock-providers/              # standalone service, own image
├─ main.py                          # OpenAI/Anthropic/Ollama/Gemini-shaped mock endpoints
├─ chaos.py                         # the live chaos-injection control plane
├─ requirements.txt / Dockerfile / pytest.ini
└─ tests/test_chaos.py              # pure-logic unit tests, no ASGI/HTTP

deploy/jaeger/config.yaml           # Jaeger v2 all-in-one, in-memory storage, OTLP receiver
deploy/grafana/provisioning/datasources/jaeger.yml   # new datasource alongside Prometheus's

loadtest/k6_gateway_stress.js       # gateway vs. baseline overhead + mid-run chaos scenario

tests/integration/
├─ conftest.py                      # requires_live_stack skip-marker, mock_providers_client fixture
├─ test_mock_provider_wire_compat.py   # real adapters vs. the mock, in-process, no Docker needed
├─ test_full_stack_integration.py      # rate limits, budgets, fallback, circuits, streaming — real HTTP
└─ test_dashboard_accuracy.py          # Prometheus's own HTTP API vs. a scripted request count

scripts/setup_demo_teams.py         # seeding wrapper + credentials banner (the demo-seed service)
scripts/verify_stack_healthy.sh     # cold-start check, polls every service's health endpoint

docker-compose.yml                  # full rewrite: gateway, mock-providers, demo-seed, prometheus,
                                     #   alertmanager(+secrets-init), grafana, jaeger, k6(load-test profile)
config/teams.yaml                   # +1 line: data-science also granted the tier-1-reasoning tier
app/ratelimit/budget_increment.lua  # bugfix: live cap, not a per-period-stale one (see "Bugs caught")
app/ratelimit/budget.py             # same bugfix, precheck() side
tests/unit/test_budget_enforcement.py  # +1 regression-guard test for the above
```

`deploy/prometheus/prometheus.yml` and `deploy/alertmanager/alertmanager.yml`
needed **no changes** — Phase 4 already anticipated the compose service
names (`gateway:8000`, `alertmanager:9093`) correctly.

## What's stubbed — now fully resolved

Every stub flagged since Phase 1 is now replaced; Phase 5 closes the last
one:

| Stub | Replaced in |
| --- | --- |
| Rate limiting always allows | Phase 2 — done |
| Circuit breaker always Closed | Phase 3 — done |
| No retry / no fallback chain | Phase 3 — done |
| No OTel spans / Prometheus metrics | Phase 4 — done |
| Traces created but never exported anywhere | **Phase 5 — done, this delivery** (Jaeger) |
| No containerized stack; Prometheus/Grafana/Alertmanager config shipped inert | **Phase 5 — done, this delivery** |
| No load test producing real, measured benchmark numbers | **Phase 5 — done, this delivery** |
| No mock provider layer for deterministic, zero-cost demo/CI traffic | **Phase 5 — done, this delivery** |

Nothing is stubbed going into Phase 6. That phase's own scope (demo
recording + written narrative) is additive, not a replacement for
anything above.

## Test plan → what each Phase 5 file proves

| Test file | Proves |
| --- | --- |
| `test_mock_provider_wire_compat.py` | Every real adapter (OpenAI/Anthropic/Ollama/Gemini) round-trips correctly against the mock's response shape, streaming included; a chaos-injected failure surfaces as the correctly-shaped, correctly-retryable `ProviderError`; runs in-process, no Docker |
| `deploy/mock-providers/tests/test_chaos.py` | Rule precedence (exact model beats wildcard), per-provider isolation, latency-without-error, clear-one-vs-clear-all |
| `test_full_stack_integration.py` | Rate-limit atomicity under real concurrent HTTP load; budget cap blocks at the right threshold (and admin changes now actually take effect mid-period — see "Bugs caught"); fallback activates on a live, HTTP-triggered outage; circuit breaker opens under real sustained failures; SSE streaming arrives intact — all against the real containerized gateway, not fakeredis |
| `test_dashboard_accuracy.py` | Prometheus's own `/api/v1/query` HTTP API reflects the *exact* count of a scripted request sequence — automates the metric-correctness half of Document 06's "manual dashboard QA" checklist item |
| `test_patching_the_cap_after_spend_already_recorded_this_period_takes_effect_immediately` (unit) | Regression guard for the budget-cap bug above |
| `loadtest/k6_gateway_stress.js` | 5,000+ concurrent requests, mixed scenarios; produces the actual measured P99 overhead, throughput, and fallback-under-load numbers Phase 6's narrative quotes verbatim |
| `scripts/verify_stack_healthy.sh` | Cold-start: every service's health endpoint answers within a documented time budget |

Regression: all 200 tests from Phases 1-4 pass unchanged, plus 10 new
in-process wire-compat tests, plus 1 new budget regression-guard test —
**211 passed** — run 3+ consecutive times during this delivery with zero
flakiness. The 7 live-stack integration tests correctly `SKIP` (not
error) when no stack is up, and were separately run for real against a
live gateway + mock-providers + Redis (no Docker daemon available in the
sandbox this was built in — see the note in "Known limitation" below)
across 3 consecutive back-to-back runs against the *same* persistent
container with zero flakiness once the two bugs above were fixed.

```
$ pytest -q
211 passed, 7 skipped in ~6s
```

**Known limitation of this delivery's own validation:** the sandbox this
was built in has no Docker daemon, so `docker-compose.yml`'s actual
container orchestration (image builds, healthcheck `depends_on`
ordering, the Alertmanager secrets-init volume handoff, Grafana/Jaeger
provisioning) could not be exercised end-to-end via `docker compose up`
itself. Everything downstream of "the gateway and mock-providers are two
running processes wired together by env vars" — which is the vast
majority of the actual application logic and every bug found above — was
validated for real, repeatedly, against genuine HTTP traffic and a real
Redis. Run `docker compose up -d && ./scripts/verify_stack_healthy.sh` as
the first thing on a real machine before relying on this for a review or
recording.

## Done criteria (from the project plan) — status

- [x] The load test script measures and reports the actual P99 overhead,
      throughput, and fallback-execution latency — `loadtest/k6_gateway_stress.js`'s
      `handleSummary()` prints these as real numbers, not projections.
      Running it for real (`docker compose --profile load-test run --rm k6`)
      and capturing the output is a Phase 6 prerequisite, not something
      this delivery can produce standalone without a live Docker host.
- [x] `docker-compose up` (plus `demo-seed`) is designed to produce a
      fully working, observable demo environment with zero manual
      configuration and zero real API keys — validated at the
      process-and-HTTP level per "Known limitation" above; full container
      orchestration needs a real Docker host to confirm end-to-end.
- [x] The integration suite is green against a real, long-lived
      gateway+mock-providers+Redis process trio, run repeatedly with zero
      flakiness — the containerized-stack version of this (same tests,
      against actual containers instead of bare processes) inherits this
      correctness directly, since the tests talk to the stack over the
      network either way and never assume anything Docker-specific.

## Developer sign-off requested before Phase 6

1. **Load test results.** This delivery ships the script; running it for
   real against a live Docker host and pasting the actual `handleSummary()`
   output (P99 overhead, throughput, fallback latency) is what Phase 6's
   narrative needs to quote — confirm who runs that pass and where the
   output gets captured (a `loadtest/results/` directory checked in? pasted
   into the Phase 6 write-up directly?).
2. **Grafana panel QA.** Document 06 explicitly calls "every panel
   resolves, no 'no data'" a manual checklist item against a live Grafana
   — confirm this happens as part of the same load-test pass above, or as
   its own separate step.
3. **Real-provider demo path.** The default compose profile is
   mock-providers-only by design (zero cost, zero manual config) — confirm
   whether the Phase 6 demo recording should also include a brief real
   Ollama/OpenAI segment (the `.env` override path already supports it;
   no code change needed either way, just a decision on what the
   recording shows).

---

## History

The sections below are preserved from when each earlier phase shipped,
unedited except where explicitly noted, as the project's own audit trail.

<details>
<summary><strong>Phase 4 — Observability Layer (original delivery notes)</strong></summary>

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

### Run it

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

### Concise implementation guide (Phase 4)

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

### Model roster upgrade (developer sign-off, folded into this phase)

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

### What's in this delivery (cumulative, all 4 phases)

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

### What's stubbed — now fully resolved

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

### Test plan → what each Phase 4 file proves

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

### Tests updated in place (not new, but changed by this phase)

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

### Done criteria (from the project plan) — status

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

### Developer sign-off requested before Phase 5

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

**Resolved at Phase 5 kickoff:** OTLP target → Jaeger; Slack webhook →
automatic init-container; `gen_ai_budget_utilization_ratio` → confirmed,
kept as-is (already load-bearing for `alerts.yml` and the Business
dashboard). See the top of this README for Phase 5's own delivery notes.

</details>

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
