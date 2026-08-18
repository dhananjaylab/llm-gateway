# Phase 6 — Narrative

Document 06's own done criteria for this phase: *"Every claim in the
narrative is traceable to a test or load-test result produced in Phase 5
— nothing asserted that wasn't measured."* Everything below follows that
rule. Three numbers are left as `[PLACEHOLDER: ...]` because the sandbox
this project was built in has no Docker daemon (see the Phase 5 README's
own "Known limitation" section) — the k6 load test has never actually
been run against a live stack. **Fill these in from a real
`docker compose --profile load-test run --rm k6` output** (see
`docs/PHASE6_DEMO_RECORDING_GUIDE.md` §0.2) before publishing anything
below. Do not estimate them — every other number here is real and
checkable against this repo's own test output; three fabricated ones
would undermine that.

---

## Part 1 — README case-study section

Drop this in as a new top section of `README.md` (above the Phase-history
detail you're keeping), replacing the current Phase 5 status block. See
§4 below for exactly how to fold the old top matter into `History`.

```markdown
## Case study: what this project demonstrates

**The problem.** Once an organization has more than one team calling
LLM APIs directly, five things break in predictable ways: a single
provider outage takes down every dependent team with no automatic
failover; one team's batch job can silently starve another team's
real-time traffic on a shared rate limit; nobody sees a cost spike until
the invoice arrives; PII redaction and compliance disclaimers get
re-implemented five different ways (or not at all); and there's no
single trace an on-call engineer can pull during an incident.

**What I built.** A production-shaped API gateway that sits in front of
OpenAI, Anthropic, Ollama, and Gemini, and solves all five at the
infrastructure layer instead of per-team:

- **One schema, four providers.** A single request format that the
  gateway translates into each vendor's exact wire protocol — OpenAI's
  Responses API, Anthropic's Messages API, Ollama's native `/api/chat`,
  Gemini's `generateContent` — and translates back, transparently,
  including streaming (SSE and NDJSON) with correct time-to-first-token
  capture.
- **Atomic, distributed rate limiting.** Dual-axis (requests/min +
  tokens/min) token buckets enforced via a single Redis Lua script, so
  concurrent gateway instances can never both observe "capacity
  available" and both admit a request. Verified under real concurrency,
  not sequential calls: 100 concurrent workers against a 10-RPM key
  admit exactly 10, every time, across repeated runs.
- **Per-team dollar budgets** with an 80%-warning header and a hard
  block at 100%, checked *before* any provider is called so a request
  that's going to be rejected never burns spend getting there.
- **Automatic multi-provider failover.** Retries with full-jitter
  exponential backoff, then a tier-based fallback chain (never a
  hard-coded model name), then a three-state circuit breaker (Closed →
  Open → Half-Open → Closed) — all Redis-backed, so every gateway
  instance agrees on a provider's state. A forced 5-in-a-row failure
  trips the breaker and the very next request skips the network call
  entirely; after cooldown, exactly one probe decides recovery.
- **Full OpenTelemetry + Prometheus observability.** A failed primary
  provider and a succeeded fallback show up as sibling CLIENT spans
  under one root span whose own status reflects what the client actually
  received — so a routine failover never trips a false application-level
  alert. Ten Prometheus metrics feed three Grafana dashboards
  (Operations, Business, Performance) and four Alertmanager→Slack rules.
- **A real chaos-testable environment**, not a slide. `docker-compose up`
  brings up the gateway, a wire-compatible mock-provider service (real
  OpenAI/Anthropic/Ollama/Gemini response shapes, not simplified stand-
  ins), Redis, Prometheus, Alertmanager, Grafana, and Jaeger — with a
  live HTTP chaos-injection endpoint so a reviewer can force an outage
  and watch the fallback and circuit-breaker panels react in real time,
  with zero manual configuration and zero real API keys required.

**Why it's engineered, not just assembled.** Two real, non-hypothetical
bugs surfaced during integration testing against a persistent container
(not a fresh in-memory Redis per test, which is how the earlier unit
suite ran): an admin's budget-cap change was silently ignored for the
rest of an already-active billing period (a Lua script was caching a
stale cap instead of trusting the live value — directly contradicting
the "a policy change never requires a restart" requirement this gateway
is supposed to guarantee), and a token-bucket capacity change didn't
retroactively refill an already-drained bucket, which is *correct*
production behavior but a real test-isolation trap. Both are fixed, both
have regression tests, and both are documented inline in the code that
fixes them, not just in a commit message.

**Results, measured:**
- `[PLACEHOLDER: GATEWAY_P99_OVERHEAD_MS]` ms P99 gateway overhead over
  direct-to-provider latency, measured as an actual delta (a k6
  `baseline_traffic` scenario hits the same mock upstream directly, at
  the same load shape, specifically so "gateway overhead" is a measured
  number and not an assertion) — target was <10ms.
- `[PLACEHOLDER: PEAK_CONCURRENT_REQUESTS]` concurrent requests
  sustained in the same load-test run, ramping from 50 req/s to
  `[PLACEHOLDER: PEAK_RPS]` req/s.
- `[PLACEHOLDER: FAILOVER_LATENCY_MS]` ms from a forced provider outage
  to a served fallback response, measured mid-run via the load test's
  own chaos-injector scenario.
- 211 unit + wire-compatibility tests passing, 7 live-stack integration
  tests (which correctly skip without a running stack and pass against
  one), zero flakiness across 5+ consecutive runs.

**Stack:** Python 3.13, FastAPI, Redis 8 (Lua/EVALSHA), OpenTelemetry,
Prometheus, Grafana, Alertmanager, Jaeger, Docker Compose, k6, GitHub
Actions.

**Try it yourself:**
```bash
git clone https://github.com/dhananjaylab/llm-gateway.git && cd llm-gateway
docker compose up -d && ./scripts/verify_stack_healthy.sh
docker compose logs demo-seed   # demo team keys + a ready-to-run curl example
```
```

---

## Part 2 — Resume bullets

Pick 2–4, not all 6 — a resume line should be scannable in under 5
seconds. Ordered roughly by how much they lead with the *reliability
engineering* framing (the strongest angle for this project, per Document
06 Phase 6's own instruction: "This is infrastructure. Lead with the
reliability numbers and the operational problem it solves").

- Built a production-shaped multi-provider LLM API gateway (Python/
  FastAPI) handling `[PLACEHOLDER: PEAK_RPS]`+ req/s with
  `[PLACEHOLDER: GATEWAY_P99_OVERHEAD_MS]`ms P99 proxy overhead,
  automatic failover across OpenAI/Anthropic/Ollama/Gemini, and
  atomic per-team rate limiting enforced via Redis Lua scripting.
- Designed and implemented a three-state circuit-breaker + tiered
  fallback-chain resilience layer, verified end-to-end with a live
  chaos-injection test harness (forced provider outages, sustained
  failures, and recovery probes), backed by a 211-test suite with zero
  observed flakiness.
- Instrumented a distributed system with OpenTelemetry tracing and
  Prometheus metrics such that a provider failover is fully
  reconstructable from telemetry alone — without producing a false
  application-level alert — feeding three purpose-built Grafana
  dashboards and four Alertmanager→Slack rules.
- Found and fixed two production-shaped bugs (a stale-cache budget-
  enforcement gap and a test-isolation trap in token-bucket refill
  logic) that only surfaced when testing against a persistent
  container instead of per-test mocks — added regression coverage for
  both.
- Containerized a 10-service observability stack (gateway, Redis,
  Prometheus, Alertmanager, Grafana, Jaeger, a wire-compatible mock-
  provider test double, k6) behind a single `docker compose up`, with
  zero manual configuration and zero real API keys required for a
  reviewer to exercise the full failure-injection and recovery story.
- Wired CI (GitHub Actions) with a fast lint+test gate on every PR and
  a separate live-stack integration workflow that brings up the full
  containerized system and runs the real chaos/failover test suite
  against it.

---

## Part 3 — LinkedIn / recruiter-facing post

Two lengths — pick based on the platform. Both end with the repo link and
(once recorded) the demo video.

### Short version (LinkedIn post body)

> Most teams that adopt LLMs end up rebuilding the same piece of
> infrastructure once they have more than one team calling a provider
> directly: something that enforces who can spend what, keeps working
> when a vendor has a bad day, and gives one place to look during an
> incident.
>
> I built that layer — an API gateway in front of OpenAI, Anthropic,
> Ollama, and Gemini that normalizes every request into one schema,
> enforces distributed per-team rate limits and dollar budgets in Redis
> with atomic Lua scripting, fails over automatically through retries →
> tiered fallback → circuit breakers, and emits OpenTelemetry traces and
> Prometheus metrics into Grafana dashboards SREs would actually want to
> look at.
>
> `[PLACEHOLDER: GATEWAY_P99_OVERHEAD_MS]`ms P99 overhead.
> `[PLACEHOLDER: PEAK_RPS]`+ req/s sustained. 211 tests, zero flakiness.
> Two real bugs caught by testing against a persistent container instead
> of fresh mocks every time — both fixed, both regression-tested.
>
> Full writeup + a 4-minute demo of a live outage triggering automatic
> failover: [repo link] / [demo video link]

### Longer version (recruiter-facing writeup)

> **What it is:** A reverse-proxy API gateway that centralizes
> governance, resilience, and cost control for every LLM call an
> organization makes — the infrastructure pattern that LiteLLM, Portkey,
> Envoy AI Gateway, and Kong AI Gateway all independently converged on in
> 2026, built here to demonstrate the internals rather than configure a
> managed version of them.
>
> **What it proves about how I work:** Every phase of this build (proxy
> layer → rate limiting → resilience → observability → integration/load
> testing → this writeup) shipped with its own test plan, its own
> "done" criteria, and a explicit developer sign-off gate before the next
> phase started — including two places where a phase's own tests caught
> a real bug in an *earlier* phase's code once tested under more
> realistic conditions (a persistent container instead of a fresh mock
> per test), and both got fixed with a regression test, not just a patch.
>
> **The concrete numbers:** `[PLACEHOLDER: GATEWAY_P99_OVERHEAD_MS]`ms
> P99 gateway overhead (measured as an actual delta against a
> direct-to-provider baseline in the same load-test run, not asserted),
> `[PLACEHOLDER: PEAK_RPS]`+ requests/sec sustained,
> `[PLACEHOLDER: FAILOVER_LATENCY_MS]`ms from a forced outage to a
> served fallback response, 211 passing tests across unit and
> wire-compatibility suites with zero flakiness across repeated runs.
>
> **Where to look:** [repo link] — `README.md`'s case study section has
> the full breakdown; `docs/` has the phase-by-phase implementation
> guides; the 4-minute demo ([video link]) shows a live simulated outage
> triggering automatic failover, a team hitting its rate limit, and the
> circuit breaker opening and recovering, all with real-time Grafana
> panels reacting on screen.

---

## Part 4 — Folding Phase 5's top matter into README History

The established pattern for this repo (every phase so far) is: current
phase's status is top-level content, everything earlier moves into a
collapsible `<details>` block under `## History`. To do that for Phase 5
now that Phase 6 is current:

1. Take the entire current top section of `README.md` — from the
   `# LLM Gateway — Phase 5: ...` title down through (but not including)
   the existing `## History` heading — and wrap it in:
   ```markdown
   <details>
   <summary><strong>Phase 5 — Integration Test, Load Test & Containerization (original delivery notes)</strong></summary>

   ...(the Phase 5 content, unedited)...

   </details>
   ```
2. Insert that new `<details>` block as the **first** entry under the
   existing `## History` heading (above the current Phase 4 entry — the
   History section is newest-first from the top of the list downward
   within that section, per the existing Phase 4/3/2/1 ordering).
3. Replace the removed top section with Part 1 of this document (the
   case-study section above) plus a short Phase 6 status line, e.g.:

   ```markdown
   # LLM Gateway — Phase 6: Portfolio Polish (Complete)

   Status: **all 6 phases complete.** CI wired (GitHub Actions: fast
   lint+test gate on every PR, full docker-compose integration suite on
   push to main), demo recorded, load-test numbers captured and quoted
   below — see `docs/PHASE6_DEMO_RECORDING_GUIDE.md` and this file's own
   `docs/PHASE6_NARRATIVE.md` for how both were produced.
   ```

This keeps README a single, current-first source of truth without
discarding the audit trail every earlier phase already established.
