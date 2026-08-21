# LLM Gateway — Phase 7+ Adoption Plan
## Analysis of External Architecture Reviews & Design for Next-Phase Roadmap

**Prepared:** August 19, 2026
**Baseline:** Phase 6 complete (221 passed / 7 skipped; CI wired; demo guide + narrative shipped with load-test placeholders pending a real `docker compose --profile load-test run --rm k6`)
**Inputs reviewed:**
1. `LLM_Gateway_Architecture_Optimization.docx` — Go-vs-Python architecture critique, token-aware rate limiting theory, resilience/observability best practice, bottleneck table, 3 strategic upgrades
2. `LLM_Gateway_Enterprise_On_Prem_Implementation_Plan.docx` — non-container (VM/systemd) enterprise deployment plan
3. `deep-research-report__2_.md` — broader enterprise deployment research (K8s/Helm alternative, LDAP/SAML, SIEM, air-gap, compliance)
4. `llm_gateway_architecture_dashboard.html` — interactive companion to Doc 1, same content plus a Go-framed "portfolio narrative" and three named upgrades (cancelled-stream accounting, semantic caching, RCU hot-reload)

**Method:** Every claim in the four source documents was checked against the actual delivered code (not the original PRD/TRD aspirations) — `app/providers/*.py`, `app/api/v1_chat.py`, `app/resilience/*.py`, `app/ratelimit/*.py`, `app/observability/*.py` — plus current (Aug 2026) web research on the specific technical claims. Findings are marked accordingly below.

---

## 1. Executive Verdict

The four documents fall into two very different categories:

- **Docs 1 & 4 (Architecture Optimization)** are mostly a *generic* tier-1-gateway best-practices review, not a review of this specific codebase. About half of what they recommend is **already built and already correct** in the delivered Phase 1–6 code (I verified this line-by-line, not by assumption — see the table in §2). The other half surfaces **two genuinely real, verifiable issues** in the current code, plus **three legitimate net-new capabilities** worth building. The document's headline recommendation — rewrite the routing plane in Go — is the one piece of advice I'm pushing back on; see §5.
- **Docs 2 & 3 (Enterprise On-Prem)** aren't an architecture critique at all — they're a **second deployment profile** (VM/systemd/enterprise-LB instead of Docker Compose) plus **enterprise identity/governance features** (OIDC, RBAC, Vault, SIEM) the gateway doesn't have yet. This is a legitimate, additive expansion, but it's a different kind of work (ops assets + auth model) and a different size of commitment than Docs 1/4. I've scoped it as a separately-gated Phase 10, not folded into the main sequence.

Given that, I designed **three new phases (7–9) that are unambiguously worth building — real bugs, real gaps, no rewrite required** — plus **one gated phase (10)** for the enterprise-identity/on-prem track, plus an explicit **"evaluated and not recommended"** section for the Go rewrite and a couple of other items. I need your sign-off on scope for 10 and on the Go question before writing any code (see the questions at the end).

---

## 2. Cross-Reference: Claim → Current Repo State → Verdict

| # | Proposed improvement (source doc) | Verified against the actual code | Verdict |
|---|---|---|---|
| 1 | Rewrite routing plane in Go for sub-2ms P99 (Doc 1, Doc 4) | Current architecture is async FastAPI/httpx with the <10ms target already the design contract (TRD hard constraint). Web research (Aug 2026) confirms Go *can* out-scale Python at very high concurrency, but also confirms the specific failure mode cited (P99 blowing up) is usually a Python-side engineering bug (blocking calls, no connection pooling), not a language ceiling — and finding #2 below shows exactly that bug present in this repo. | **Don't rewrite. Fix the real bug (below) first; see §5.** |
| 2 | "Socket exhaustion (TIME_WAIT) at 5,000+ RPS... naive HTTP transport constructing new TCP connections" (Doc 1's own bottleneck table, Phase 5 row) | **Confirmed, real bug.** Every adapter (`openai_adapter.py`, `anthropic_adapter.py`, `ollama_adapter.py`, `gemini_adapter.py`) does `async with httpx.AsyncClient(...) as client:` **inside** `call()`/`stream()` — a brand-new client (and connection pool) is constructed and torn down on *every single request*. This is a textbook anti-pattern (confirmed against current httpx docs and multiple 2026 production write-ups). | **Real. → Phase 7, build task 1.** |
| 3 | Estimate-Admit-Reconcile token reservation pattern | Already the exact design of `app/ratelimit/estimator.py` + `limiter.py` + `reconcile_tpm.lua`, built in Phase 2. | **Already done.** |
| 4 | Atomic Lua-script rate limiting to avoid MULTI/WATCH races | Already `token_bucket.lua` via `EVALSHA` (Phase 2). | **Already done.** |
| 5 | Cancelled/aborted stream token accounting — "financial leakage" when a client disconnects mid-stream (Doc 1, Doc 4 "Upgrade 1") | **Confirmed, real gap.** In `app/api/v1_chat.py::_stream_response`, a client disconnect triggers `break`, which falls into the `try/else` block with `final_usage is None` → `actual_tokens = 0` (full TPM refund) **and** `budget_enforcer.record_spend()` is never called. Tokens the client actually received before disconnecting are never billed. | **Real. → Phase 7, build task 2.** |
| 6 | Passive sliding-window health telemetry instead of costly active polling (Doc 1) | Already the architecture: `CircuitBreaker` gates traffic off passive request outcomes; `HealthChecker`'s active probing is a separate, **optional** (`HEALTH_CHECK_ENABLED`), observability-only signal that never gates routing (see `health.py`'s own module docstring). | **Already done — matches the recommendation almost exactly.** |
| 7 | Mid-stream SSE failure must emit an explicit error frame, never corrupt the stream (Doc 1) | Already exactly this: `event: error` SSE frame in `_stream_response`'s `except` branch; fallback is explicitly scoped to *before* the first chunk only. | **Already done.** |
| 8 | Bounded Prometheus label cardinality (no user_id/prompt_hash/req_id) (Doc 1) | Already true — `GatewayMetrics` labels are `team_id, provider, model, status/type` only. | **Already done.** |
| 9 | Config hot-reload without restart (RCU pattern) (Doc 1/4 "Upgrade 3") | Already built for **team config** (`TeamConfigStore` + Redis pub/sub, Phase 2). **Not** built for `config/tiers.yaml` (fallback chains) or `config/pricing.yaml` — both load once at boot and never watch for changes. | **Partially real gap. → Phase 9, build task 1.** |
| 10 | Semantic caching layer (Doc 1/4 "Upgrade 2") | Explicitly out-of-scope in the original PRD ("Nice to Have... deferred"). Not built. Redis 8 (already the pinned version) ships native vector sets (`VADD`/`VSIM`) as of 8.0 — confirmed via current docs — so this doesn't require a new service, just new code. | **Real, legitimate net-new capability. → Phase 9, build task 2 (opt-in, gated carefully — see risks below).** |
| 11 | Tool/function-calling translation across providers (Doc 1) | Not built at all — `UnifiedChatRequest` has no `tools` field. Confirmed current (Aug 2026) OpenAI `tool_calls`/Anthropic `tool_use`/Gemini `functionCall` shapes all still diverge exactly as described. | **Real, meaningful capability gap. → Phase 8.** |
| 12 | Structured-output/JSON-schema handling differences (esp. Gemini) (Doc 1) | `ResponseFormat` only has `text`/`json_object`, no schema passthrough or per-provider translation. | **Real gap. → Phase 8.** |
| 13 | Hierarchical (org→team→app→user→model) quota enforcement (Doc 1) | Only team-level quotas exist today. | **Real gap. → Phase 8.** |
| 14 | Combine RPM+TPM+budget into one Redis round trip (Doc 1 bottleneck table) | RPM+TPM are already combined in one Lua script; **budget precheck is a separate round trip** before it. | **Real but minor. → Phase 7, build task 3 (marked optional/stretch).** |
| 15 | Circuit breaker: percentage error-rate + partial canary traffic in Half-Open, vs. fixed-count + single probe (Doc 1) | Current implementation is fixed-count (5-in-10) + single probe — which the project's **own TRD Appendix A already flagged** as the deliberate v1 choice, with percentage-mode named as the v2 extension. | **Legitimate v2 extension, already anticipated by the project. → Phase 7, build task 4 (config-toggle, not a replacement).** |
| 16 | `gen_ai.usage.cost_usd` / `llm.fallback.triggered` span attributes (Doc 1) | Cost is computed and used for metrics/budget but **never attached to the CLIENT span**. Fallback is inferable from the span tree shape already (2 CLIENT spans) but not a literal boolean. | **Minor real gap. → Phase 7, build task 5.** |
| 17 | OIDC/OAuth2/JWT auth, RBAC by role, Vault secrets, SIEM export, non-container (systemd/Ansible/NGINX/HAProxy) deployment (Docs 2 & 3) | None of this exists — the gateway only has flat hashed API keys and Docker Compose. | **Real, large, additive scope. → Phase 10 (gated, needs your sign-off on breadth).** |
| 18 | LDAP/AD, SAML (Doc 3) | Not built. | **Redundant with OIDC for this project's purposes — see §5.** |
| 19 | True air-gapped tooling: internal package mirrors, internal CA, offline SBOM (Docs 2 & 3) | Not built; mostly org process, not app code. | **Document the pattern in Phase 10; don't build infrastructure no reviewer can verify. See §5.** |

---

## 3. Track A — Recommended, Python-native, additive to the existing 6 phases

No rewrite. No new required infrastructure service (Redis 8 is already pinned and already supports what's needed). Same phase-gating discipline as Phases 1–6: goal → build tasks → implementation guide → test plan → done criteria, sign-off before the next phase starts.

### Phase 7 — Performance & Financial-Correctness Hardening
**Depends on:** Phase 6. **Timebox estimate:** 2–3 days.
**Goal:** Fix the two concrete, verified defects the external review surfaced (connection pooling, cancelled-stream billing), and close the two smaller gaps it correctly identified as already-anticipated v2 extensions in the project's own TRD.

**Build tasks:**
1. **Shared, pooled `httpx.AsyncClient` per provider adapter.** Each adapter (`OpenAIAdapter`, `AnthropicAdapter`, `OllamaAdapter`, `GeminiAdapter`) constructs **one** `httpx.AsyncClient` at `__init__` (with `httpx.Limits(max_connections=.., max_keepalive_connections=..)`, env-configurable), holds it for process lifetime, and `call()`/`stream()` reuse it instead of `async with httpx.AsyncClient(...)`. Wire `aclose()` into `app/main.py`'s lifespan shutdown via the existing `registry.py` `@lru_cache`'d adapter singletons. This is the fix for bottleneck #2 in the table above — no architecture change, no new dependency, directly addresses the "5,000+ RPS socket exhaustion" claim with the actually-correct root cause.
2. **Partial-stream token accounting on client disconnect.** Buffer output text as SSE chunks are forwarded (already touching each chunk in `_stream_response`'s loop — this adds accumulation, not a new pass). On `is_disconnected()`, if no terminal usage chunk has arrived yet, estimate partial output tokens from the buffer using the existing char-ratio heuristic (`app/ratelimit/estimator.py`'s pattern, reused rather than duplicated), reconcile the TPM refund against that estimate instead of a blanket full refund, and call `budget_enforcer.record_spend()` for the partial cost. Document the estimate as approximate (heuristic, not exact tokenization) directly in the code, matching the project's existing "estimates are corrected by reconciliation, exactness isn't the point" philosophy.
3. *(Optional/stretch)* Combine the budget precheck into the same Lua script as the RPM/TPM check, cutting one Redis round trip off the hot path. Flagged optional because it touches two modules whose current separation (`budget.py` precheck is deliberately read-only/side-effect-free) was a considered design choice — worth doing, but not blocking Phase 7 sign-off if time-boxed out.
4. **Circuit breaker percentage-mode (config toggle).** Add an error-rate-percentage variant alongside the existing fixed-count mode (`CIRCUIT_BREAKER_MODE=fixed_count|error_rate`, default unchanged = `fixed_count`), resolving the TRD Appendix A's own flagged v2 extension. No change to default behavior or existing tests.
5. **`gen_ai.usage.cost_usd` on the CLIENT span.** One-line addition to `set_span_success` in `app/observability/tracing.py`.

**Test plan:** `test_provider_connection_pooling.py` (assert exactly one `AsyncClient` constructed across N concurrent calls to the same adapter — regression guard for the exact bug found); extend `test_reservation_reconciliation.py` and `test_streaming_passthrough.py` for partial-disconnect billing (assert `record_spend` called with tokens ≈ partial buffer estimate, not 0); extend `test_circuit_breaker.py` for percentage mode (new tests, existing fixed-count tests untouched); extend `test_span_attributes.py` for `cost_usd`. Full existing 221-test suite re-run as regression.

**Done criteria:** All 221+ tests green; new connection-pooling test proves the fix under real concurrency (`asyncio.gather`, same technique as `test_token_bucket_concurrency.py`); a scripted mid-stream disconnect test shows non-zero recorded spend; percentage-mode circuit breaker demonstrated without touching the fixed-count default's existing tests.

---

### Phase 8 — Provider Capability Parity: Tool Calling, Structured Outputs, Hierarchical Quotas
**Depends on:** Phase 7. **Timebox estimate:** 4–6 days (the largest of the three — real schema work across 3 adapters).
**Goal:** Close the two biggest genuine capability gaps between this gateway and what a real multi-provider deployment needs to proxy, plus extend quota enforcement above team-level.

**Build tasks:**
1. **Schema evolution.** Add `tools: list[ToolDefinition] | None`, `tool_choice`, and extend `ChatMessage` to carry `tool_calls`/`tool_call_id`/a `tool` role, without breaking the existing "system is not a message role" pattern already established in `schema.py`.
2. **Adapter translation, one provider at a time** (same ordering discipline as Phase 1 — OpenAI first, Anthropic second because it forces the top-level-field-style difference, Gemini third):
   - OpenAI: `tools` array passthrough; normalize `tool_calls` (JSON-string args) → parsed object in the unified schema; round-trip via `role: "tool"` messages.
   - Anthropic: `tools` with `input_schema`; normalize `tool_use` content blocks; round-trip via `tool_result` content blocks (parsed object args, not string — a real structural difference from OpenAI that the adapter must handle).
   - Gemini: `functionDeclarations` (proto-style STRING/NUMBER/OBJECT, not full JSON Schema); normalize `functionCall` parts; round-trip via `functionResponse` parts.
3. **Structured output translation.** Extend `ResponseFormat` with a `json_schema` variant; translate to OpenAI's `text.format={"type":"json_schema",...}` and Gemini's `response_schema`. Anthropic has no native strict-schema mode as of this research pass — document this as a known cross-provider capability gap (gateway either 400s clearly if a team requests strict-schema mode against an Anthropic-only chain, or falls back to tool-call-based extraction, config-gated) rather than silently degrading.
4. **Hierarchical quotas.** Add an optional org-level bucket evaluated in the *same* Lua script as the team-level RPM/TPM check (one more `KEYS`/`HASH` pair, same atomic pattern — not a second round trip), admin-adjustable the same way team limits already are.

**Test plan:** Extend `deploy/mock-providers/main.py` to optionally emit tool-call-shaped responses (request-flag-gated, so none of the existing 10 wire-compat tests need to change); new `test_tool_calling_translation.py` parametrized across all three providers for a shared tool definition, translating in both directions; new `test_structured_outputs.py`; new `test_hierarchical_quotas.py` proving an org-level cap can independently deny even when the team-level bucket has headroom.

**Done criteria:** At least one full tool-call round trip (request → provider tool_use/tool_calls → gateway normalizes → client responds with tool result → gateway forwards correctly) passing per provider against the mock service; structured-output translation tested for OpenAI and Gemini, Anthropic gap explicitly documented and tested (clear error, not silent failure); hierarchical quota test demonstrates the two-tier breach scenario; full regression suite green.

---

### Phase 9 — Hot-Reload Extension & Semantic Caching
**Depends on:** Phase 8. **Timebox estimate:** 3–5 days.
**Goal:** Extend the project's own already-proven Redis-pub/sub hot-reload pattern to routing/pricing config, and add an opt-in, carefully-isolated semantic cache using Redis 8's native vector sets — no new infrastructure service.

**Build tasks:**
1. **`TiersConfigStore` / `PricingStore`, mirroring `TeamConfigStore` exactly.** Same idiom already established: Redis is the runtime source of truth, `tiers.yaml`/`pricing.yaml` are bootstrap seeds only, Admin API (`PATCH /admin/tiers`, `PATCH /admin/pricing`) mutates live, Redis pub/sub invalidates every instance's cache — deliberately the *same* mechanism as team config rather than introducing a second, filesystem-watcher-based hot-reload path. (A filesystem/ConfigMap watcher is noted as the right choice specifically for the Kubernetes/on-prem deployment profile in Phase 10, where Redis may not always be the routing-policy source of truth in some enterprise setups — documented there, not built twice here.)
2. **Semantic cache, opt-in per team** (mirrors the existing `pii_redaction`/`system_prompt_prefix` per-team policy flags — off by default):
   - **Embedding:** FastEmbed (ONNX Runtime, no PyTorch dependency — confirmed via research to be materially lighter than `sentence-transformers` for a container this size), small model (~384-dim class), exact model pinned at implementation kickoff per the project's "search first" convention.
   - **Storage:** Redis 8 native vector set (`VADD`/`VSIM`), **one vector set per team+model-tier**, never shared across tenants.
   - **Cache key correctness (the part that actually matters):** the embedded/looked-up content must be the *post-policy* request — i.e., hashed/embedded **after** PII redaction and system-prompt injection are applied, and the cache key must include team_id + tier + a hash of the effective system prompt. A request with `pii_redaction: true` must never be able to hit a cache entry created under `pii_redaction: false`, and team A must never see team B's cached response even for byte-identical prompts.
   - **Two-layer lookup:** exact-match (hash of the normalized post-policy request) checked first, semantic (cosine similarity) only on exact-miss — cheaper and zero-risk-of-wrong-answer for the common repeat-question case.
   - **Threshold:** per-team configurable, conservative default (0.95+); below ~0.90 is a documented false-positive risk per current industry guidance.
   - **Scope:** non-streaming, cacheable only when the team's policy opts in; a TTL (config, default e.g. 24h) governs entry lifetime, respecting the same "don't retain more sensitive data than necessary" posture the project's PII redaction already establishes.
   - **New metrics:** `gen_ai_cache_lookup_total{team_id,layer,result}` and a cost/latency-saved figure, wired the same way the existing 10 metrics are; **new tracing:** a cache-check INTERNAL span before the (skipped-on-hit) CLIENT span.

**Test plan:** Hot-reload round trip for tiers/pricing (`PATCH /admin/tiers/{tier}` takes effect on the very next request, zero restart — same shape as the existing `test_patch_limits_takes_effect_on_the_very_next_request`); semantic cache tests using a deterministic `FakeEmbedder` test double (no real ONNX model needed in unit tests, same philosophy as `FakeAdapter`) covering: cache hit/miss, cross-tenant isolation (team A's entry never returned to team B), policy-mismatch isolation (PII-on vs PII-off never share an entry), TTL expiry, and the exact-before-semantic ordering.

**Done criteria:** tiers/pricing hot-reload proven with zero restart, same rigor as Phase 2; semantic cache demonstrably reduces provider calls in a scripted repeated-prompt integration test; cross-tenant and policy-mismatch isolation tests both pass (these are the two tests that matter most — a cache that leaks across tenants or policy states is worse than no cache); feature fully opt-in, zero behavior change for any team that doesn't enable it; full regression suite green.

---

## 4. Track B — Phase 10: Enterprise Identity, RBAC & On-Prem Deployment Profile

**Resolved at roadmap kickoff (Aug 19, 2026): full scope, scheduled.** Not gated on a future decision — it ships after Phase 9, same as any other phase in the sequence, subject to the same per-phase sign-off gate.

This is where Docs 2 & 3 live. It's real, additive, and doesn't conflict with the Docker Compose stack already shipped — Doc 2's own stated philosophy ("deployment technology should remain below the application architecture... add deployment manifests/containers around the same application contracts rather than rewriting the gateway") means this is genuinely a **second deployment profile**, not a replacement.

It's also, honestly, **2–3x the size of Phases 7–9 combined**, and its value is mostly *breadth of deployment story* rather than new request-handling logic — flagged here so the size is visible going in, not a reason to reconsider.

**Build tasks:**
1. **OIDC/JWT auth as a second, parallel mode** alongside the existing hashed-API-key model (not a replacement — service-to-service traffic keeps using keys; human/enterprise-app traffic gets JWTs). `PyJWT` + `PyJWKClient` for JWKS-based verification (confirmed current recommendation over `python-jose`, which has an unmaintained dependency chain).
2. **RBAC**: roles (`platform_admin` / `ai_team` / `application_team` / `auditor`) from JWT claims or a role field on `TeamConfig`, gating admin endpoints and model access more granularly than today's flat `allowed_models` list.
3. **Pluggable secrets backend**: `SecretsProvider` interface, env-var implementation (today's default, unchanged) plus a HashiCorp Vault (`hvac`) implementation.
4. **Non-container deployment assets**: systemd unit file(s), an Ansible playbook, NGINX/HAProxy reverse-proxy config templates — genuinely alternative to, not a rewrite of, `docker-compose.yml`.
5. **SIEM/audit export**: forward the existing Redis Stream audit log to an external sink (syslog or HTTP webhook).
6. **Air-gap/restricted-egress**: documented as a pattern (network diagram, offline-artifact build path, SBOM generation) rather than literally standing up an air-gapped mirror no reviewer could verify anyway.

---

## 5. Evaluated and NOT Recommended

- **Full Go rewrite of the routing plane.** **Resolved at roadmap kickoff (Aug 19, 2026): skip it, stay Python.** The concrete bottleneck the source documents point to (§2, row 2) is a Python-level bug with a well-known fix, not a language ceiling — and fixing it is far higher return than a rewrite that would discard 6 shipped phases, 221 passing tests, and the whole existing measured-narrative story (`docs/PHASE6_NARRATIVE.md`). If a Go benchmark is ever wanted, it's a small, honestly-scoped **separate** side project, not folded into this repo's identity.
- **Full LDAP/SAML.** Redundant with OIDC for what this project needs to demonstrate; worth naming as a "documented extension point" in Phase 10 rather than building.
- **Literal air-gapped tooling** (internal package mirrors, internal CA, offline registries). Mostly organizational process, not application code, and not independently verifiable in a portfolio context — document the pattern, don't build the infrastructure.

---

## 6. Roadmap Decisions — Resolved at Kickoff (Aug 19, 2026)

Per the project's own established pattern (TRD Appendix A), these were left as explicit questions rather than assumptions, and are now resolved:

1. **Go rewrite** — **skip entirely, stay Python.** See §5.
2. **Phase 10 scope** — **full breadth, scheduled** (not deferred, not trimmed). See §4.
3. **Where to start** — **Phase 7 build kicks off now**, scoped via the usual 3-clarifying-questions pass (see the companion Phase 7 kickoff scoping).

Sequence going forward: **Phase 7 → 8 → 9 → 10**, each gated on sign-off of the prior phase's done criteria, unchanged from the project's Phase 1–6 discipline.

---

## 7. Research Basis (this pass, Aug 19 2026)

- httpx connection-pooling anti-pattern and fix: httpx official docs (`python-httpx.org/async`), multiple 2026 production write-ups (Baseten, OneUptime, Medium) all converge on the same "one client, held for process lifetime" pattern already used elsewhere in this project's own architecture docs.
- Tool-calling schema shapes (OpenAI `tool_calls`/Anthropic `tool_use`/Gemini `functionCall`): cross-checked against 6+ independent Aug 2026 guides; shapes match what Doc 1 described.
- Redis 8 native vector sets (`VADD`/`VSIM`): confirmed via `redis.io/docs` and `redis-py` client docs — available since Redis 8.0.0, no new service required given the project already pins `redis:8-alpine`; `fakeredis[lua]` (already a pinned test dependency) has vector-set command support, so the existing test-double pattern extends cleanly. **Re-verify the exact `fakeredis` version range still covers this at Phase 9 kickoff.**
- FastEmbed (Qdrant, ONNX Runtime, no PyTorch dependency) recommended over `sentence-transformers` for embedding generation — meaningfully lighter for this project's lean-container ethos; exact model to pin at Phase 9 kickoff.
- `PyJWT` + `PyJWKClient` recommended over `python-jose` for JWT/OIDC verification — `python-jose` carries an unmaintained `ecdsa` dependency with an unfixed advisory; multiple 2026 FastAPI-community threads confirm the same conclusion.
- FastAPI/Python async proxy P99 reality check: current (2026) evidence is mixed-but-clear — Python *can* hit sub-10ms/high-RPS targets with correct engineering (pooling, avoiding blocking hot-path work), and at least one named competitor gateway (LiteLLM, also Python/FastAPI) is reported hitting real P99 problems at 500+ RPS — consistent with "implementation discipline matters more than language" rather than "Python is disqualified."

All version numbers above are directional for planning purposes; per the project's own working agreement, exact versions get pinned via a fresh web search at the start of whichever phase actually builds each piece.
