# LLM Gateway — Phase 7: Performance & Financial-Correctness Hardening
## Concise Implementation Guide

**Status:** built, tested, passing (**255 passed, 8 skipped** — up from Phase 6's 221/7; regression-clean).
**Source:** external architecture review (`PHASE7_PLUS_ADOPTION_PLAN.md`, roadmap kickoff Aug 19 2026), scoped via the usual 3-question pass, then verified line-by-line against the actual delivered code rather than assumed.
**Depends on:** Phase 6 (complete). **Delivered:** Aug 21 2026.

This phase fixes two real, verified defects an external architecture review surfaced (not generic advice — each was confirmed against the actual repo before any code was written), plus closes two gaps the project's own TRD Appendix A had already anticipated as v2 extensions.

---

## Build tasks — what shipped

### 1. Connection pooling fix (the real bug)

Every adapter (`OpenAIAdapter`, `AnthropicAdapter`, `OllamaAdapter`, `GeminiAdapter`) used to do `async with httpx.AsyncClient(...) as client:` **inside** `call()`/`stream()` — a brand-new client, and therefore a brand-new TCP connection pool, constructed and torn down on every single request. This is the concrete root cause behind "socket exhaustion (TIME_WAIT) at 5,000+ RPS" the review flagged, confirmed by reading the code, not assumed.

**Fix:** each adapter now builds **one** pooled `httpx.AsyncClient` in `__init__`, with `httpx.Limits(max_connections=.., max_keepalive_connections=..)` — held for the adapter's lifetime, reused across every call. Connection limits are **env-configurable per provider** (`OPENAI_MAX_CONNECTIONS`, `OPENAI_MAX_KEEPALIVE_CONNECTIONS`, etc. — 8 new settings in `ProviderSettings`, matching the class's existing per-provider `api_key`/`base_url` pattern), per explicit kickoff scoping.

`ProviderAdapter` gained a non-abstract `aclose()` hook (default no-op, so `FakeAdapter` needed zero changes) that each real adapter overrides to close its client. `app/providers/registry.py::close_all_adapters()` closes every cached adapter's client; wired into `app/main.py`'s lifespan shutdown.

**Known, documented limitation:** `registry.reset_registry_cache()` (test-only, called by the autouse fixture between every test) does **not** close evicted adapters' clients — it's synchronous, every call site is synchronous, and threading async cleanup through it would ripple into many existing test fixtures for a benefit that's at most a benign `ResourceWarning` in a test process that exits shortly after anyway. Production teardown (one real process lifetime) always goes through the async `close_all_adapters()` path instead. Stated explicitly in the code, not silently omitted.

### 2. Advanced token accounting for cancelled and aborted streams (the real financial-correctness gap)

Before this phase, a client disconnect mid-stream — or a provider failing after some content had already reached the client — meant `_stream_response` fully refunded the TPM reservation **and never called `budget_enforcer.record_spend()` at all**. Tokens the client actually received were never billed. Confirmed by reading the code (`final_usage is None` → `actual_tokens = 0`), not assumed.

**Fix:** `_stream_response` now accumulates the delta text actually forwarded to the client. A new shared helper, `_reconcile_and_bill_partial`, computes a best-effort `Usage` from what's actually known when no terminal usage chunk arrived — the same pre-flight input-token estimate already used for the reservation, plus a new per-provider **output**-token estimate — and reconciles/bills against that instead of a blanket zero. Applied consistently to **both** the disconnect path and the mid-stream-provider-failure path (both were previously affected).

**Per-provider tokenizer strategy** (`app/ratelimit/output_tokenizer.py`) — scoped via kickoff to "most accurate available, heavier dependencies OK," then adjusted once actual availability was checked by hand:

| Provider | Method | Why |
|---|---|---|
| OpenAI | `tiktoken` | Real, offline, official BPE tokenizer |
| Gemini | `google-genai`'s local `SentencePiece` tokenizer | Real, offline (no network fetch), official — confirmed working end-to-end |
| Anthropic | Anthropic's own documented ~3.5-chars/token heuristic | **Anthropic publishes no offline tokenizer at all** — confirmed against their current docs. The only exact option is a network round trip to their `count_tokens` API, the wrong trade for a best-effort cleanup path already mid-teardown on a dropped connection. This is Anthropic's own suggested fallback ratio, not a shortcut invented here — consistent with the project's existing "no fabricated precision" principle. |
| Ollama / anything else | The project's existing 4-chars/token heuristic | No single tokenizer across arbitrary community-uploaded local models; Ollama is $0/token per `config/pricing.yaml` anyway, so precision here only affects the TPM refund, never billing. |

Every failure mode — network unavailable, package missing, unrecognized model string, an SDK internal error — degrades cleanly to the heuristic. Never raises. This matters in practice: **`tiktoken` fetches its encoding table from `openaipublic.blob.core.windows.net` on first use per process** (not bundled in the wheel); in a restricted-egress environment (see the upcoming Phase 10 on-prem track) that fetch fails unless `TIKTOKEN_CACHE_DIR` is pre-warmed at image build time. Confirmed by actually running it in this environment, not assumed — the corresponding test (`test_openai_uses_the_real_tiktoken_encoding_when_reachable`) skips gracefully when unreachable, same pattern the project's own `requires_live_stack` marker already established for "needs external reachability" tests.

### 3. Combined budget + rate-limit check (single Redis round trip)

Budget precheck and the RPM/TPM token bucket used to be two sequential Redis round trips on the hot path. `combined_check.lua` evaluates both atomically in one `EVALSHA` — budget first, read-only, and only falls through to the (duplicated, not sub-script-called — Redis Lua has no cross-script call primitive without a second round trip) token-bucket logic if budget allows, preserving the exact ordering/atomicity guarantee the two-call version had.

**The real design risk here, found during implementation, not assumed:** naively combining these into one Redis call would break the project's own deliberate, already-tested asymmetry — budget fails **closed** on a Redis outage, rate limiting fails **open** (TRD Appendix A). A single combined call can't fail each domain independently. **Fix:** `CombinedQuotaChecker.check()` treats the combined script as a fast path only; on any Redis connectivity error, it falls back to running the exact two original separate calls, preserving both postures byte-for-byte. The combined script only ever changes behavior on the Redis-healthy path — never on the failure path.

**Scope boundary:** only used for normal/high-priority requests. Batch-priority requests keep their existing separate budget precheck + `BatchPriorityQueue` polling loop unchanged — that loop already retries the rate-limit check itself over up to `BATCH_QUEUE_MAX_WAIT_SECONDS`, which a single combined round trip isn't shaped for.

**Verified against the full existing test suite, not just new tests:** the strict, concurrency-sensitive `test_token_bucket_concurrency.py` (exactly-N-of-100-concurrent-requests-succeed) and `test_budget_enforcement.py` (80%-warning-exactly-once, hard-cap-at-402) both pass unchanged through the new combined path — the Lua script produces byte-identical observable behavior to the two-script version.

### 4. Circuit breaker error-rate (time-windowed) mode

The TRD's own Appendix A anticipated this: "move to a rolling error-rate percentage only if the fixed threshold proves too twitchy." `CIRCUIT_BREAKER_MODE=error_rate` (default remains `fixed_count`, zero behavior change unless explicitly enabled) trips the circuit on error rate over a **time** window (last N seconds — a Redis sorted set scored by timestamp, reusing the exact rolling-window pattern `app/resilience/health.py`'s `HealthTracker` already established) instead of a **request-count** window (last N calls), which behave very differently under bursty or sparse traffic.

`circuit_check.lua` — the Closed/Open/Half-Open transition logic itself — needed **zero changes** and is reused unmodified for both modes: it only reads `state`/`opened_at`/`probe_in_flight`, never the failure-counting window. Only `circuit_record.lua` needed a time-windowed sibling (`circuit_record_error_rate.lua`), using a distinct Redis key (`...:er_window`, a ZSET) from the fixed-count mode's LIST-backed `...:window`, so a deployment that ever switches modes at runtime never hits a `WRONGTYPE` error against leftover data from the other mode.

Includes a `minimum_samples` floor (default 5) — without it, a single failure in a freshly-reset window reads as "100% error rate" and trips immediately, which is the opposite of what a percentage mode is supposed to fix.

### 5. `gen_ai.usage.cost_usd` on the CLIENT span

Cost was computed for budget/metrics but never attached to the trace span itself. `FallbackRouter` gained an optional `pricing_table` constructor param (`None` by default — every pre-Phase-7 direct `FallbackRouter(...)` construction in the test suite keeps working unmodified); when present, cost is computed **inside** the span's `with` block (same reasoning Phase 4 already established for why `translate_response()` lives in `fallback.py` — a span can't be enriched after it closes) for both the non-streaming and streaming paths.

---

## What's explicitly *not* in this phase

Per roadmap kickoff: no Go rewrite (the actual bottleneck was a Python-level bug with a known fix, not a language ceiling — see `PHASE7_PLUS_ADOPTION_PLAN.md` §5). Phase 8 (tool calling, structured outputs, hierarchical quotas), Phase 9 (hot-reload extension, semantic caching), and Phase 10 (enterprise identity/RBAC/on-prem deployment, scheduled at full scope) remain.

---

## New dependencies (search-first pass, Aug 19 2026 — pin dates in `requirements.txt`)

| Package | Version | Purpose |
|---|---|---|
| `tiktoken` | `>=0.14,<0.15` | OpenAI offline tokenizer for partial-stream accounting |
| `google-genai` | `>=2.19,<2.20` | Only its `local_tokenizer` submodule is used — the gateway's own `GeminiAdapter` stays hand-rolled httpx against the raw REST API, on purpose, unchanged |
| `sentencepiece` | `>=0.2,<0.3` | `google-genai`'s local tokenizer's own runtime dependency |

No new *infrastructure* dependency — Redis 8 (already pinned) needed no changes for any Phase 7 build task.

## New environment variables (see `.env.example` for full context/comments)

`OPENAI_MAX_CONNECTIONS`, `OPENAI_MAX_KEEPALIVE_CONNECTIONS`, `ANTHROPIC_MAX_CONNECTIONS`, `ANTHROPIC_MAX_KEEPALIVE_CONNECTIONS`, `GEMINI_MAX_CONNECTIONS`, `GEMINI_MAX_KEEPALIVE_CONNECTIONS`, `OLLAMA_MAX_CONNECTIONS`, `OLLAMA_MAX_KEEPALIVE_CONNECTIONS`, `CIRCUIT_BREAKER_MODE`, `CIRCUIT_BREAKER_ERROR_RATE_WINDOW_SECONDS`, `CIRCUIT_BREAKER_ERROR_RATE_THRESHOLD`, `CIRCUIT_BREAKER_ERROR_RATE_MINIMUM_SAMPLES`. All have defaults matching pre-Phase-7 behavior — nothing needs to be set for the gateway to keep working exactly as before.

---

## Test plan → what each new/changed file proves

| File | Proves |
|---|---|
| `test_provider_connection_pooling.py` (new, 6 tests) | The client is built exactly once at `__init__`, not per call, across all 4 adapters; 20 concurrent calls reuse it; two adapter instances never share a client; `aclose()`'s base-class no-op default doesn't break `FakeAdapter`-shaped subclasses |
| `test_output_tokenizer.py` (new, 10 tests, 1 skips without network) | Anthropic's own heuristic ratio (not the generic default); Ollama/unknown fallback; Gemini's real local tokenizer (always runs — no network needed); OpenAI's real `tiktoken` (skips gracefully without network, matching `requires_live_stack`'s precedent); every load/encode failure mode degrades to the heuristic instead of raising; loaders are cached per model, not reloaded every call |
| `test_streaming_passthrough.py` (updated) | The disconnect test's expected reconcile/bill values updated from the old blanket-zero to the new partial-accounting behavior — an intentional behavior change, not a regression |
| `test_fallback_chain.py` (+1 test) | A mid-stream provider failure now bills the partial content actually sent, verified end-to-end through the real budget ledger via the Admin API |
| `test_combined_quota_checker.py` (new, 7 tests) | Fast-path allow/deny correctness; budget denial never touches the rate-limit buckets (atomicity); the Redis-outage fallback still enforces budget fail-closed *and* rate-limit fail-open independently; the fallback's separate calls do **not** run on the healthy path (spy-verified) |
| `test_circuit_breaker_error_rate.py` (new, 8 tests) | `minimum_samples` floor prevents single-blip trips; opens correctly at/above threshold; stays closed below threshold regardless of sample count; failures age out of the time window; ZSET members stay unique under same-timestamp bursts (the exact bug class the script's own header warns about); Half-Open/probe recovery is unchanged from `fixed_count` mode (shared `circuit_check.lua`); Redis-outage fail-open; `fixed_count` remains the default when `mode` isn't specified |
| `test_span_attributes.py` (+3 tests) | `cost_usd` attaches correctly for both non-streaming and streaming (terminal-chunk) paths; a `FallbackRouter` built without `pricing_table` omits the attribute rather than crashing |

**Full regression: 255 passed, 8 skipped** (221 baseline + 34 new tests; the 1 additional skip is the network-dependent `tiktoken` test, correctly skipping in a restricted-egress environment and expected to run for real in GitHub Actions CI, which has normal internet access).

## Done criteria — status

- [x] All pre-Phase-7 tests pass unchanged in *behavior* (two tests updated in-place because Phase 7 *intentionally* changed the behavior they assert — flagged above, not silently patched)
- [x] Connection-pooling fix proven under real concurrency (`asyncio.gather`, same technique as `test_token_bucket_concurrency.py`)
- [x] A scripted mid-stream disconnect and a scripted mid-stream provider failure both now result in non-zero recorded spend matching the partial content actually sent
- [x] Combined quota check verified byte-identical to the two-call version across the *entire* existing rate-limit/budget test suite, not just new tests written against it
- [x] Percentage-mode circuit breaker fully additive — zero changes to `fixed_count` mode's existing tests or default behavior
- [x] `ruff check` clean on every file this phase touched (pre-existing findings in files this phase didn't touch — e.g. `app/resilience/health.py`'s one `RUF046` — are out of scope, already tracked in `docs/PHASE6_CI_IMPLEMENTATION_GUIDE.md`)

## Open item for developer sign-off

`README.md` is still titled "Phase 5" — Phase 6 added CI wiring and demo docs but never executed its own documented instruction to fold Phase 5's content into a `History` section (see `docs/PHASE6_NARRATIVE.md` §4). Not touched by this delivery (out of scope for a Phase 7 code change, and "deliver only changed files" argues against a drive-by rewrite of an unrelated document) — flagged here rather than silently left for someone to notice later. Worth a dedicated pass whenever Phase 6 and 7's README sections both get folded in together.
