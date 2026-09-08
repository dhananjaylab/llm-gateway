# LLM Gateway — Phase 8b: Streaming Tool Calls
## Concise Implementation Guide

**Status:** built, tested, passing (**346 passed, 8 skipped** — up from Phase 8's 328/8; regression-clean, zero
flakiness across 3 consecutive full-suite runs).
**Source:** `docs/PHASE8B_KICKOFF_SCOPING.md`, delivered exactly as scoped — every open decision in that doc's §8
was resolved per its own stated recommendation (no scope changes mid-build).
**Depends on:** Phase 8 (complete, 328/8, independently re-verified — `PHASE8_SIGNOFF.md`).

**Baseline verified before any code was written, not assumed:** cloned `dhananjaylab/llm-gateway` fresh, confirmed
`main` was at Phase 8's merge commit (`c50c9cd`, PR #29), ran the real suite (328 passed / 8 skipped, matching
`PHASE8_SIGNOFF.md` exactly) before touching anything.

---

## Build tasks — what shipped

### 1. Schema evolution (`app/core/schema.py`)

`ToolCallDelta` (new): `index` (opaque per-response correlation key, not guaranteed contiguous), `id`/`name`
(populated only on the first delta for a given index), `arguments_delta` (always a JSON-string fragment, even
for a provider that hands over the whole string in one shot). `UnifiedStreamChunk` gained
`tool_call_deltas: list[ToolCallDelta] | None`.

Phase 8's blanket `tools + stream=True` schema-level rejection is **removed, not relaxed** — the schema layer
can't know at construction time whether a tier name will resolve to a provider that supports streaming tool
calls. Capability enforcement now lives entirely in the adapter layer, where every other per-provider capability
gap in this codebase already lives.

### 2. Three adapters gain streaming tool-call parsing; one gains nothing

| Provider | What changed | What didn't need to |
|---|---|---|
| OpenAI | `stream()` now handles `response.output_item.added` (declares `call_id`/`name`) and `response.function_call_arguments.delta` (JSON-string fragments); terminal `finish_reason` is `"tool_calls"` whenever any function-call item was seen | `response.function_call_arguments.done` — deliberately unused; concatenating `.delta` fragments already reconstructs the identical string |
| Anthropic | `stream()` now handles `content_block_start` (previously **entirely unhandled** — a genuine gap this phase closed, not just an addition) to declare `tool_use` blocks, and `input_json_delta` fragments on `content_block_delta` | `_map_stop_reason`'s `"tool_use" → "tool_calls"` mapping — already added in Phase 8 for the non-streaming path; the streaming terminal chunk already called this function, so the correct behavior was inherited for free |
| Ollama | `stream()` now detects `message.tool_calls` per NDJSON line and re-emits each as a single, complete `ToolCallDelta` (one delta per call, not fragments) | The parsing logic itself — reuses `_extract_tool_calls()` **completely unchanged**, the same helper the non-streaming path already used |
| Gemini | **Nothing.** `stream()` continues to unconditionally raise `ProviderError(501, "unsupported_streaming")` | Confirmed via a live end-to-end test (`test_full_chain_exhausted_by_three_fakes_then_gemini_bubbles_its_own_501_not_a_generic_exhaustion`) that this is correct, sufficient enforcement with zero new code |

### 3. A real, easy-to-miss interaction, confirmed by testing rather than assumed

Gemini's streaming rejection is **non-retryable**. When `tier-1-reasoning`'s real chain (`openai → anthropic →
ollama → gemini`) is walked with the first three links failing retryably, the walk reaches Gemini as the last
link and its non-retryable `ProviderError` **aborts the chain walk immediately** — it does **not** get
reclassified as a generic `fallback_chain_exhausted` 503, even though every link ultimately failed. This is
Document 03's existing "a bad request is a bad request on every provider" rule applying to "streaming isn't
implemented here" exactly the way it already applies to a genuine 400/401/403 — not a new rule, but a
non-obvious consequence worth a dedicated test (`test_streaming_tool_calls.py`), since a developer extending this
chain later could reasonably expect "every link failed" to always mean the same 503 shape.

### 4. `app/resilience/fallback.py` — confirmed untouched

`FallbackRouter.stream_with_fallback`'s commit-to-this-link boundary (`await gen.__anext__()`) commits on
receiving *any* first chunk, never inspecting its content. A tool-call-carrying first chunk is handled correctly
by this existing mechanism with zero code changes — confirmed by
`test_fallback_activates_for_a_streaming_tools_request_before_any_chunk` retrying and falling back exactly like
the pre-existing text-only case.

### 5. Partial-stream billing extension (`app/api/v1_chat.py`)

`_stream_response`'s `accumulated_text` now also absorbs `chunk.tool_call_deltas[].arguments_delta`, not just
plain `chunk.delta` text. A disconnect or mid-stream failure while a tool call's arguments are still streaming
now bills the partial argument text actually sent, via the same char-ratio/tiktoken/Anthropic-heuristic estimator
Phase 7 already built — closing the same class of financial-leakage gap Phase 7 fixed for plain text, for
effectively free. Verified end-to-end (`test_mid_stream_failure_during_tool_call_bills_the_partial_arguments_sent`):
budget spend strictly increases after a mid-argument-accumulation failure, where it would previously have been
zero.

### 6. Test infrastructure: `FakeAdapter` gains `tool_call_chunks`

`tests/unit/conftest.py`'s shared `FakeAdapter` test double gained an optional `tool_call_chunks:
list[list[ToolCallDelta]]` constructor parameter, letting fallback/billing/SSE-passthrough tests exercise the
*real* HTTP pipeline with genuine `tool_call_deltas` content without a mocked SSE body. A latent, previously
unreachable gotcha was fixed in the same pass: `stream_chunks=[]` used to silently fall back to the default three
text chunks (`stream_chunks or [...]` treats an empty list as falsy) — never triggered before this phase, since
no caller ever passed an intentionally empty list; fixed to `is not None`, which changes nothing for any existing
caller.

### 7. Mock-provider tool-call streaming fixtures (`deploy/mock-providers/main.py`)

Closes the exact open item Phase 8's own implementation guide flagged ("tool-call mock responses are still
unbuilt"). All three streaming endpoints (`/openai/v1/responses`, `/anthropic/v1/messages`, `/ollama/api/chat`)
now emit tool-call-shaped SSE/NDJSON when the request carries `tools`, echoing back a call to whatever tool the
request actually offered (not a hardcoded name) — gated purely on `payload.get("tools")`, so every one of the 10
pre-existing wire-compat tests (which never send `tools`) is provably unaffected
(`test_mock_streaming_without_tools_is_unaffected_by_the_new_branch`). Non-streaming tool-call mock responses
remain unbuilt — that gap predates this phase and is explicitly out of its scope (streaming specifically).

Four new wire-compat tests (`tests/integration/test_mock_provider_wire_compat.py`) prove each real adapter
correctly parses the mock's new tool-call streaming output — the same "strongest available check" this file's
own docstring describes, run in-process with no Docker needed.

---

## What's explicitly *not* in this phase

Gemini streaming of any kind (§8 Q1 of the kickoff doc — a separate, larger, pre-existing gap) · non-streaming
tool-call mock responses in `deploy/mock-providers/main.py` (Phase 8's own already-flagged, still-open item) ·
any change to org/quota/budget logic beyond the partial-billing extension already described · gateway-side
buffering of an in-progress tool call before forwarding (§8 Q4 — a truncated argument fragment on a mid-stream
failure is documented as the client's problem to detect and discard, consistent with how streaming is supposed
to work).

---

## New/changed files (this delivery only)

```
app/core/schema.py                                # ToolCallDelta, UnifiedStreamChunk.tool_call_deltas,
                                                    #   removed the tools+stream schema-level guard
app/providers/openai_adapter.py                    # streaming tool-call event parsing
app/providers/anthropic_adapter.py                 # content_block_start handling (new) + input_json_delta
app/providers/ollama_adapter.py                    # streaming tool_calls detection, reuses _extract_tool_calls()
app/api/v1_chat.py                                 # accumulated_text absorbs tool-call argument fragments
deploy/mock-providers/main.py                      # tool-call-shaped streaming fixtures, request-flag-gated
tests/unit/conftest.py                             # FakeAdapter.tool_call_chunks + stream_chunks=[] fix
tests/unit/test_streaming_tool_calls.py            # NEW — 14 tests
tests/unit/test_tool_calling_translation.py        # 1 test flipped (intentional, documented behavior change)
tests/integration/test_mock_provider_wire_compat.py  # +4 tests

docs/PHASE8B_KICKOFF_SCOPING.md                    # design/scoping doc (pre-existing from kickoff)
docs/PHASE8B_IMPLEMENTATION_GUIDE.md                # this file
```

`app/providers/gemini_adapter.py` and `app/resilience/fallback.py` are **not in this list** — both confirmed,
by design and by test, to need zero changes.

Full regression: **346 passed, 8 skipped** (328 baseline + 18 new: 14 in `test_streaming_tool_calls.py`, 4 in
`test_mock_provider_wire_compat.py`), zero flakiness across 3 consecutive runs. `ruff check` clean on every file
this phase touched; the 14 pre-existing findings elsewhere in the repo are untouched and unrelated (same list
`PHASE8_SIGNOFF.md` already documented).

```
$ pytest -q
346 passed, 8 skipped in ~20s
```

---

## Done criteria — status

- [x] All pre-Phase-8b tests pass unchanged in behavior (328/8 baseline fully intact within the new 346/8 total,
      except the one explicitly-flagged intentional flip)
- [x] Per-provider streaming tool calls reconstruct byte-identically to the equivalent non-streaming `ToolCall` —
      verified directly against each real adapter via `httpx.MockTransport`-mocked wire fixtures
- [x] Parallel tool calls attributed correctly by index (OpenAI, Anthropic)
- [x] Interleaved text + tool call handled correctly in one stream (Anthropic)
- [x] `finish_reason` is `"tool_calls"` exactly when a tool call streamed, `"stop"` otherwise, for all three
      providers, including a text-only-despite-tools-being-offered regression guard
- [x] Fallback/retry confirmed to need zero code changes, proven by a live pre-first-chunk fallback test with
      tools present
- [x] Gemini's exclusion confirmed both in isolation and via the real 4-link tier chain, proving its non-retryable
      abort — not a generic chain-exhaustion — is what actually happens
- [x] Partial-stream billing extended and proven to increase spend on a mid-tool-call-argument failure
- [x] Mock-provider streaming tool-call fixtures built and proven against every real adapter, closing Phase 8's
      own flagged open item
- [x] `ruff check` clean on every file this phase touched

## Open items for developer sign-off before the next phase

1. **Ollama parallel tool calls in streaming** (kickoff doc §8 Q3) — still not independently re-verified against
   a live Ollama install; the implementation assumes calls are enumerated within whichever single chunk carries
   `message.tool_calls`. Recheck before this is ever demoed against real Ollama.
2. **Full live-stack integration** (`docker compose up` + `tests/integration/test_full_stack_integration.py`-style
   coverage of streaming tool calls against the real containerized mock-providers service) is still blocked on
   this sandbox's missing Docker daemon — same known limitation Phase 5's README already documents. The
   in-process wire-compat tests added this phase are the strongest check available without it.
3. **What comes next** — with streaming tool calls closed, the original Phase 9 scope
   (`docs/PHASE7_PLUS_ADOPTION_PLAN.md` §3: tiers/pricing hot-reload + semantic caching) and Phase 10 (enterprise
   identity/RBAC/on-prem) remain exactly as previously scoped and un-started.
