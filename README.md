# LLM Gateway — Phase 1: Unified Proxy Layer

Status: **built, tested, passing (34/34)**. Timebox per the plan: Day 1-3.

This is Phase 1 of 6 in the LLM Gateway project (see the project's own
Document 06, Implementation Plan). Phase 1's goal, verbatim from that doc:
> "one internal request/response shape, three provider adapters behind it,
> and a working streaming passthrough — before any rate limiting or
> resilience logic exists."

Rate limiting, budgets, circuit breakers, fallback chains, and OTel/
Prometheus observability are all **intentionally absent**. Their call
sites are wired (see "What's stubbed" below) so Phases 2–4 are drop-in
replacements, not refactors.

---

## Run it

```bash
python -m venv .venv && source .venv/bin/activate      # or .venv\Scripts\activate on Windows
pip install -r requirements-dev.txt
cp .env.example .env                                     # fill in whichever provider keys you have
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

Add `"stream": true` to see the SSE passthrough (`curl -N ... | cat`).

Run the test suite:

```bash
pytest -v
```

## What's in this delivery

```
app/
├─ main.py                    # FastAPI app factory, /healthz, /readyz
├─ api/
│  └─ v1_chat.py               # POST /v1/chat/completions, GET /v1/models
├─ core/
│  ├─ schema.py                # unified request/response Pydantic models
│  ├─ auth.py                  # X-Gateway-API-Key -> TeamConfig (hashed lookup)
│  ├─ config.py                # teams.yaml loader + provider env-var settings
│  └─ policy.py                # system-prompt injection + regex PII redaction
├─ providers/
│  ├─ base.py                  # ProviderAdapter interface + ProviderError
│  ├─ registry.py               # "openai:gpt-5.4" -> (adapter, "gpt-5.4")
│  ├─ openai_adapter.py
│  ├─ anthropic_adapter.py
│  └─ ollama_adapter.py
├─ ratelimit/stub.py           # permissive no-op — Phase 2 replaces this file
└─ resilience/stub.py          # permissive no-op — Phase 3 replaces this file

config/teams.yaml              # 3 demo teams (data-science, product-eng, batch-devs)
scripts/hash_api_key.py        # hash a raw key for teams.yaml
tests/unit/                    # 34 tests, see mapping below
```

## Design decisions made while building (flagged per the "search first" working agreement)

1. **`max_completion_tokens` for OpenAI, not `max_tokens`.** Current-gen
   GPT-5.x chat models expect this field name. Re-verify against
   `platform.openai.com/docs/api-reference/chat` before pinning a model
   family, since OpenAI's model lineup has moved fast (GPT-5.4 → 5.5 → 5.6
   in the few months before this was written).
2. **Ollama adapter targets the native `/api/chat` endpoint, not the
   `/v1/chat/completions` OpenAI-compat shim.** This is what the TRD's
   normalization table (`options.num_predict`, `options.stop`) describes,
   and it's what forces the adapter to actually handle a third distinct
   wire format (NDJSON, not SSE) — the OpenAI-compat shim would have let
   this adapter get away with reusing OpenAI's SSE-parsing code, which
   defeats the point of building it third.
3. **Streaming uses a hand-formatted `StreamingResponse`, not FastAPI's new
   native `fastapi.sse.EventSourceResponse` (shipped in 0.135+).** This was
   evaluated first, since it's the current idiomatic way to do SSE in
   FastAPI — but its encoding only activates for a route whose *function
   itself* is statically a generator (`response_class=EventSourceResponse`
   decided at route registration). This endpoint streams or not based on
   the client's `stream` field in the request body, which isn't known
   until the function is already running, so a single function can't both
   `return` JSON and `yield` SSE. See the version-note in
   `app/api/v1_chat.py` for the full reasoning.
4. **Team API keys are hashed (`sha256:...`) in `teams.yaml` from Phase 1
   on**, not deferred to Phase 2, since Document 05's security note makes
   clear raw keys should never be persisted, and retrofitting hashing
   later would mean re-provisioning every demo team anyway.
5. **`requirements.txt` stays on classic `httpx`, not the new
   Pydantic-stewarded `httpx2`.** `httpx2` exists and is API-compatible
   (`AsyncClient`, `.stream()`, `.aiter_lines()` all match), but
   `respx`/`pytest-httpx` — the transport-mocking libraries Phase 2/3's
   integration tests will need — are built against classic `httpx`, and
   Starlette's own `TestClient` already emits a deprecation warning
   pointing at `httpx2` (observed directly in this build's test output).
   **Open decision for developer sign-off:** migrate `app/providers/*` and
   the dev dependencies to `httpx2` once respx/pytest-httpx confirm
   support — it's a near drop-in given the matching async API.

## What's stubbed (by design) and where it gets replaced

| Stub | File | Replaced in |
| --- | --- | --- |
| Rate limiting always allows | `app/ratelimit/stub.py` | Phase 2 (Redis token bucket) |
| Circuit breaker always Closed | `app/resilience/stub.py` | Phase 3 (Redis-backed state machine) |
| No retry / no fallback chain | (provider errors bubble up as 502) | Phase 3 |
| `teams.yaml` loaded once, no hot-reload | `app/core/config.py` | Phase 2 (Admin API + watcher) |
| No OTel spans / Prometheus metrics | — | Phase 4 |
| Model id must be `provider:model` literal | `app/providers/registry.py` | Phase 3 (abstract tiers + fallback chains) |

## Test plan → what each file proves

| Test file | Proves |
| --- | --- |
| `test_schema_normalization.py` | One `UnifiedChatRequest` translates correctly into all three providers' native request shapes (system placement, max-tokens key, stop-sequence key) |
| `test_response_normalization.py` | All three providers' distinct response shapes normalize back to the same `UnifiedChatResponse` |
| `test_auth.py` | Valid key → reaches the adapter; invalid key → 401; valid key + disallowed model → 403; unknown provider prefix → 400 |
| `test_streaming_passthrough.py` | SSE chunks arrive in order/unmodified; TTFT is logged on the first chunk; a mid-stream client disconnect stops consuming from and closes the upstream generator (proven by chunk-count and an explicit `aclose()` check) |
| `test_policy_injection.py` | PII redaction and system-prompt injection apply only when a team's policy enables them, and never mutate the caller's original request object |

```
$ pytest -v
======================== 34 passed in 0.4s ========================
```

## Done criteria (from the project plan) — status

- [x] A single client payload produces a correct request to all three
      providers, verified by adapter unit tests, not manual curl checks.
- [x] Streaming and non-streaming both work end-to-end (verified against
      the FakeAdapter test double at the HTTP layer; run the `curl`
      commands above against a real Ollama/OpenAI/Anthropic key for a
      live check — no API keys are available in this build environment,
      see "Developer sign-off" below).
- [x] No provider SDK type appears outside `app/providers/` — nothing
      beyond `httpx` (a generic HTTP client, not a provider SDK) is
      imported outside that package; confirmed by grep.

## Developer sign-off requested before Phase 2

This build environment has no live provider credentials or a local Ollama
install, so streaming/non-streaming were verified against the `FakeAdapter`
test double exercised through the real FastAPI/Starlette HTTP stack (auth,
policy, routing, SSE framing, disconnect handling) rather than a live
network call. Two things worth your sign-off before Phase 2 starts:

1. **Run one real request** against whichever provider you have a key for
   (command above) to confirm the adapter's wire format matches reality —
   adapter unit tests use fixture payloads built from current provider
   docs, but nothing beats one live round-trip.
2. **Confirm the `httpx` vs. `httpx2` call** (decision #5 above) — stay on
   classic `httpx` for now, or move early?

Phase 2 (Rate Limiting and Budget Enforcement, Day 3-6) starts from here:
Redis token buckets, budget caps, priority queues, and the Admin API.
