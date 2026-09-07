# LLM Gateway — Phase 8b: Streaming Tool Calls
## Kickoff Scoping & Architecture Document

**Status:** design/scoping only — **no application code shipped in this pass**. Per this project's own working
agreement (stop for developer sign-off before starting the next phase), this document is that sign-off gate.
Build starts once §8's four questions are answered.

**Baseline:** Phase 8 signed off complete (`PHASE8_SIGNOFF.md`) — 328 passed / 8 skipped, independently
re-verified against the merged repo, three consecutive clean runs, zero drift between delivered and merged code.
Phase 8 shipped **non-streaming tool calling only**, by explicit sign-off, with a schema-level guard rejecting
`tools=[...] + stream=True` at construction time. This phase closes that gap.

**Source:** `docs/PHASE8_IMPLEMENTATION_GUIDE.md`'s own "Open items for developer sign-off," item 1, and
`docs/PHASE8_KICKOFF_SCOPING.md` §5 ("Explicitly deferred out of this phase... streaming tool calls become an
explicitly named Phase 8b or Phase 9 stretch item"). This document is that named follow-up, re-scoped against
fresh web research (Sept 2026) on the actual current streaming wire formats — Phase 8's own scoping doc explicitly
deferred this research rather than guessing at it, and that deferral is what this pass resolves.

---

## 0. Research finding that changes the plan

The four providers are **not symmetric** for streaming tool calls, and the asymmetry is structural, not a detail
to normalize away:

| Provider | Streaming tool-call support | Argument delivery |
|---|---|---|
| OpenAI (Responses API) | Yes, mature, current | Incremental JSON-string fragments (`response.function_call_arguments.delta`) |
| Anthropic (Messages API) | Yes, mature, current | Incremental JSON-string fragments (`input_json_delta.partial_json`) |
| Ollama (native `/api/chat`) | Yes, confirmed via official docs | **Whole parsed object in one chunk** — no fragment accumulation on the wire at all |
| Gemini (`generateContent`) | **No baseline streaming of any kind exists in this codebase** | N/A |

Three findings drove the design below, each confirmed against current (Sept 2026) sources, not assumed:

1. **OpenAI's streaming tool-call shape is a near-exact streaming mirror of its own non-streaming shape.**
   `response.output_item.added` (item type `function_call`, carries `call_id`/`name`) declares the call;
   `response.function_call_arguments.delta` streams `arguments` as JSON-string fragments (`item_id`, `delta`);
   `response.function_call_arguments.done` gives the finalized string. `arguments` was already a JSON string in
   this codebase's canonical `ToolCall` shape (see `schema.py`'s own docstring) — no new object↔string conversion
   needed for OpenAI's streaming path, only incremental delivery of the same string.

2. **Anthropic's streaming tool-call shape is `content_block_start` (declares `id`/`name` via a `tool_use`
   content block) → repeated `content_block_delta` events of type `input_json_delta` carrying `partial_json`
   fragments → `content_block_stop`.** Anthropic's own docs are explicit that these fragments are not
   token-by-token — "current models only support emitting one complete key and value property from input at a
   time" — but the wire contract is still incremental and must be buffered/concatenated by the consumer exactly
   the way this codebase's own non-streaming `AnthropicAdapter.translate_response` already does with the *whole*
   `input` object (just one buffering step earlier). `message_delta.delta.stop_reason == "tool_use"` reuses the
   `_map_stop_reason` table Phase 8 already extended for the non-streaming path — no new mapping needed, only a
   new call site.

3. **Ollama's native `/api/chat` streams tool calls as a complete parsed object per chunk, not JSON fragments** —
   confirmed via `docs.ollama.com/capabilities/streaming` and independently via a third-party multi-provider
   dialect implementation that states this explicitly ("Tool call arguments arrive complete (as a map, not
   streamed JSON fragments), similar to Google Gemini"). This is actually a **simplification**: Ollama's
   streaming adapter can reuse the existing non-streaming `_extract_tool_calls()` helper completely unchanged,
   then re-emit its result as a single "delta" that happens to carry the whole argument string at once — no
   fragment-buffering logic needed for this provider at all.

   A second, unrelated confirmation while researching this: Ollama's **OpenAI-compatible `/v1` shim** has a
   known, still-open bug (GitHub issue #5769, reconfirmed in a May 2026 field report) where streaming silently
   drops `tool_calls` delta chunks entirely. This codebase's `OllamaAdapter` has always pointed at the native
   `/api/chat` endpoint, never the `/v1` shim (see that adapter's own module docstring) — this finding doesn't
   change anything, it just confirms that earlier, unrelated design choice was the right one and would have been
   a silent landmine had it gone the other way.

4. **Gemini has no baseline streaming implementation to extend.** `GeminiAdapter.stream()` has unconditionally
   raised `ProviderError(status_code=501, error_type="unsupported_streaming")` since Phase 1 — for text, let
   alone tool calls. A true incremental function-call-argument streaming feature does exist
   (`streamFunctionCallArguments: true` in `toolConfig.functionCallingConfig`, Gemini 3+), but it is documented
   under Google Cloud's **Vertex AI / "Gemini Enterprise Agent Platform"** docs, not confirmed as available on
   the public Gemini Developer API (`generativelanguage.googleapis.com`) this adapter targets. Building Gemini
   streaming — of any kind — is a separate, larger, already-flagged gap that predates tool calling entirely.
   Folding it into "streaming tool calls" would silently double this phase's scope. **Recommendation: Gemini
   stays fully out of scope; its existing 501 behavior is unchanged.** See §8 Q1.

---

## 1. Goal

Replace Phase 8's schema-level `tools + stream=True` rejection with real streaming tool-call support for the
three providers whose streaming protocols support it today (OpenAI, Anthropic, Ollama), using one unified,
provider-agnostic delta shape a client can accumulate identically regardless of which provider actually served
the request — continuing this codebase's own established normalization philosophy (Document 02's protocol
normalization table, Phase 8's `ToolCall.arguments` always-a-string convention) rather than introducing a
provider-specific escape hatch.

---

## 2. Schema evolution — `app/core/schema.py`

Additive only, same non-negotiable this codebase has applied to every prior schema change. Two changes:

### 2.1 `ToolCallDelta` (new) + `UnifiedStreamChunk.tool_call_deltas`

```python
class ToolCallDelta(BaseModel):
    """
    One incremental update to one in-progress tool call within a streaming
    response. `index` is a stable per-call correlation key for THIS
    response only (not a global id) — every provider's own protocol
    already gives us one (OpenAI's `output_index`, Anthropic's
    `content_block` index); the gateway reuses it directly rather than
    renumbering, so it is not guaranteed contiguous or zero-based when a
    response also contains ordinary text output items interleaved with
    function calls.

    `id`/`name` are populated ONLY on the first delta emitted for a given
    index — every later delta for that index carries `arguments_delta`
    only and leaves both None. This mirrors OpenAI's own client-side
    accumulation convention almost exactly (the same "index" concept,
    the same declare-once-then-stream-body shape), which is deliberate:
    it keeps this gateway's wire contract familiar to anyone who has
    already built a client against OpenAI-style streaming tool calls.

    `arguments_delta` is always a JSON-string FRAGMENT to append to
    whatever has already been accumulated for this index — true even for
    a provider (Ollama) that hands over the complete arguments in one
    chunk: that case just means index N receives exactly one non-empty
    delta instead of several. No consumer of this stream — the gateway's
    own fallback-cutoff check, or an external client — ever has to branch
    on which provider is serving the request to know how to accumulate.
    """
    index: int
    id: str | None = None
    name: str | None = None
    arguments_delta: str = ""


class UnifiedStreamChunk(BaseModel):
    ...  # unchanged fields
    tool_call_deltas: list[ToolCallDelta] | None = None
```

**Client-side reconstruction contract** (documented here since it's the wire contract, not just an implementation
detail): group deltas by `index`; the first delta seen for an index carries `id` + `name`; concatenate
`arguments_delta` across every delta sharing that index, in arrival order; once a chunk with
`finish_reason == "tool_calls"` arrives, every index accumulated so far is one complete `ToolCall` with
`arguments = "".join(fragments)` — byte-identical to what a non-streaming call to the same request would have
produced (verified in the test plan, §7).

### 2.2 Remove the blanket schema-level guard

Phase 8's `UnifiedChatRequest._tools_not_yet_supported_with_streaming` model_validator (rejects `tools +
stream=True` unconditionally, at construction time) is **removed**, not relaxed. The alternative — teaching the
schema layer which providers currently support streaming tool calls — is impossible to do correctly at
validation time: `model` may be a *tier name* (`"tier-1-reasoning"`) resolved into a provider chain later, so
the schema layer cannot know at construction time whether the eventual provider will support this combination.

Capability enforcement moves to exactly where every other per-provider capability gap in this codebase already
lives — the adapter itself. `GeminiAdapter.stream()` already unconditionally raises `ProviderError(501,
"unsupported_streaming")` for **any** streaming attempt, tools or not. That single existing line is now also
correct and sufficient tool-call-streaming capability enforcement for Gemini, with zero new code: a
`tools=[...], stream=True` request whose tier chain includes Gemini (as a fallback link, e.g.
`tier-1-reasoning`'s chain ends with `gemini:gemini-3.6-flash`) will hit that same `ProviderError` exactly the
way any other Gemini streaming attempt already does today, and the existing fallback/retry machinery already
knows what to do with it. **No fallback.py changes are required for this** — see §5.

---

## 3. Per-provider translation

### 3.1 OpenAI (`app/providers/openai_adapter.py`)

New event handling inside the existing `stream()` SSE loop (which already dispatches on `event_name`):

| Event | Action |
|---|---|
| `response.output_item.added`, `item.type == "function_call"` | Record `pending[output_index] = (call_id, name)`. Yield nothing yet. |
| `response.function_call_arguments.delta` | Look up `pending[output_index]`; if this is the first delta seen for that index, yield `ToolCallDelta(index=output_index, id=call_id, name=name, arguments_delta=delta)` and mark the index as declared; every subsequent delta for the same index yields `id=None, name=None`. |
| `response.completed` (terminal, existing handler) | `finish_reason` becomes `"tool_calls"` if any `function_call` items were seen this stream, else the existing `"stop"` — same priority rule the non-streaming path already applies (`"tool_calls" if tool_calls else _extract_responses_finish_reason(raw)`), just evaluated incrementally instead of from a full response body. |

`response.function_call_arguments.done` is **not** needed as a second source of truth — the concatenated
`.delta` fragments already reconstruct the identical string, and treating `.delta` as the sole source keeps the
adapter itself free of any local buffering beyond the `pending` index→(id,name) map every `stream()` call already
needs regardless.

### 3.2 Anthropic (`app/providers/anthropic_adapter.py`)

The existing `stream()` loop currently does **not** handle `content_block_start` at all (only
`message_start`/`content_block_delta`/`message_delta`/`message_stop`). This phase adds it:

| Event | Action |
|---|---|
| `content_block_start`, `content_block.type == "tool_use"` | Record `pending[index] = (content_block["id"], content_block["name"])`. Yield nothing yet. |
| `content_block_delta`, `delta.type == "input_json_delta"` | Same declare-on-first-delta pattern as OpenAI: first delta for `index` carries `id`/`name` from `pending`, every later one carries only `arguments_delta = delta["partial_json"]`. |
| `content_block_delta`, `delta.type == "text_delta"` | Existing behavior, unchanged (a response can freely interleave a text block and a tool_use block at different indices — both need independent handling in the same loop). |
| `message_delta`, `delta.stop_reason == "tool_use"` | `_map_stop_reason` already maps `"tool_use" → "tool_calls"` (added in Phase 8 for the non-streaming path) — the streaming terminal chunk already calls `_map_stop_reason(stop_reason)`, so this is a zero-new-code correctness inheritance, not a new mapping. |

### 3.3 Ollama (`app/providers/ollama_adapter.py`)

Per NDJSON line, if `message.tool_calls` is present: reuse the existing `_extract_tool_calls()` helper
**unchanged** (it already returns fully-formed `ToolCall` objects with synthesized `gwsyn_{index}` ids, exactly
as the non-streaming path does), then convert each result into `ToolCallDelta(index=i, id=call.id,
name=call.name, arguments_delta=call.arguments)` — one complete delta per call, no follow-up fragments will
arrive for that index. Track a `saw_tool_calls` flag across the whole stream (rather than assuming tool calls
only ever arrive on the final `done: true` line) so `finish_reason="tool_calls"` is set correctly regardless of
which line actually carried them.

### 3.4 Gemini (`app/providers/gemini_adapter.py`)

**No change.** `stream()` continues to raise `ProviderError(status_code=501, error_type="unsupported_streaming")`
unconditionally, for every streaming attempt. See §0 finding 4 and §8 Q1.

---

## 4. Partial-stream billing extension

`app/api/v1_chat.py::_reconcile_and_bill_partial` currently accumulates `accumulated_text` from `chunk.delta`
(text) only. A client disconnect or mid-stream provider failure *during* tool-call argument accumulation is real
generated output that should count toward the partial-token estimate the same way partial text already does —
otherwise a tool-call-heavy stream that gets cut short silently under-bills to zero for the tool-call portion,
reopening exactly the financial-leakage class of bug Phase 7 already fixed for plain text. Mechanical fix: also
append the concatenation of every `tool_call_deltas[].arguments_delta` seen so far into `accumulated_text`. This
reuses the existing char-ratio/tiktoken/Anthropic-heuristic output-token estimator
(`app/ratelimit/output_tokenizer.py`) unchanged — it was already a best-effort estimate, not exact tokenization,
so folding tool-call text into the same estimate is strictly more accurate than leaving it at zero, for
effectively free.

---

## 5. Fallback / retry / circuit-breaker interaction — confirmed, no code change required

`FallbackRouter.stream_with_fallback`'s commit-to-this-link boundary is `await gen.__anext__()` inside
`_start_link` — it commits on **receiving any first chunk at all**, never inspecting that chunk's content. A
first chunk that happens to carry `tool_call_deltas` instead of a text `delta` is already handled correctly by
this existing mechanism with zero changes: retries and fallback still apply fully *before* that first chunk
arrives (a provider that fails to even start a tool-calling stream still gets retried, then failed over to the
next chain link, exactly like today), and the existing "no fallback after content has reached the client, only
the `event: error` mid-stream path" rule from Document 03 already covers a tool-call delta the same way it
already covers a text delta — because it was never actually keyed on "was it text," only "did a chunk arrive."

One genuinely new failure shape this phase introduces, not present in Phase 1–8: a mid-stream failure *while a
tool call's arguments are still being accumulated* leaves the client holding an incomplete, unparseable JSON
fragment for that one tool call, inside an `event: error` frame the same shape Phase 1 already established. This
is a client-side concern (discard any tool call whose index never received a chunk with `finish_reason ==
"tool_calls"`), not something the gateway should try to solve by buffering an entire tool call server-side before
forwarding — that would defeat the purpose of streaming for tool-call-heavy responses and add complexity
disproportionate to the benefit. Flagged explicitly for sign-off rather than silently decided — see §8 Q4.

---

## 6. Observability — no schema change, one nice-to-have flagged

The existing CLIENT span (`app/observability/tracing.py::set_span_success`) needs no new required attribute —
`gen_ai.response.model`/`.usage.*` already cover a tool-calling streamed response identically to a text one. A
`gen_ai.response.finish_reasons` or a tool-call-count attribute would be a reasonable observability nice-to-have
but isn't load-bearing for anything this phase's own done criteria need — proposed as an optional stretch item,
not a build task.

---

## 7. Test plan

| File | Proves |
|---|---|
| `tests/unit/test_streaming_tool_calls.py` (new) | Per-provider (OpenAI, Anthropic, Ollama), a shared tool definition streamed end-to-end reconstructs byte-identically to the equivalent non-streaming `ToolCall` fixture already established in `test_tool_calling_translation.py` — same name, same `arguments` string, after client-side accumulation by `index` |
| — parallel tool calls | Two concurrent tool calls in one response, different `index` values, deltas correctly attributed and never cross-contaminated — for OpenAI and Anthropic (both confirmed to support this); Ollama parallel-call streaming is flagged for a build-time live-instance recheck, not blocking design sign-off (§8 Q3) |
| — interleaved text + tool call | A response streaming some text then a tool call (or vice versa) produces correctly-typed chunks in arrival order, for OpenAI and Anthropic |
| — `finish_reason` correctness | `"tool_calls"` exactly when tool deltas were seen this stream, `"stop"` otherwise, for all three providers |
| `tests/unit/test_fallback_chain.py` (extended) | A streaming+tools request whose primary fails before any chunk still retries then falls back correctly (extends the existing pre-first-chunk fallback test with a tool-call variant); a chain that exhausts into Gemini still cleanly surfaces the existing 501 `unsupported_streaming` error — confirming this phase does not change that behavior |
| `tests/unit/test_streaming_passthrough.py` / a new tool-call variant | Mid-stream failure during tool-call argument accumulation produces the existing `event: error` SSE shape, and partial billing (§4) reflects the argument fragments actually sent |
| `tests/unit/test_schema_normalization.py` (updated in place) | `UnifiedChatRequest(tools=[...], stream=True)` no longer raises at construction — replaces Phase 8's `test_tools_plus_streaming_is_rejected_at_the_schema_boundary`, flipped to assert acceptance. Flagged explicitly as an **intentional, documented behavior change**, matching this project's own precedent (Phase 7's two in-place test updates for the disconnect-billing behavior change) — not a silent regression |
| `deploy/mock-providers/main.py` (extended) | Tool-call-shaped streaming SSE/NDJSON fixtures, request-flag-gated so the 10 existing wire-compat tests need zero changes — flagged in Phase 8's own "open items" as blocked on this sandbox's missing Docker daemon for full live-stack verification, same known limitation carried forward |
| Regression | Full 328+8 baseline re-run green; no existing test's *behavior* changes except the one explicitly flagged above |

---

## 8. Open decisions requiring sign-off before build starts

1. **Gemini streaming — confirm staying entirely out of scope.** Its own baseline streaming gap predates tool
   calling and is bigger than this phase; folding it in here would silently double the phase's scope for a
   provider that's a late-chain fallback link in every current tier, not a primary. Recommendation: leave the
   existing 501 behavior untouched; scope general Gemini streaming as its own future item if/when it's wanted.
2. **Partial-stream billing for tool-call fragments (§4)** — fold argument-fragment text into the existing
   char-ratio output-token heuristic (small, effectively free), or leave tool-call-heavy partial streams
   under-billed and flag as a known limitation? Recommendation: fold it in — it directly closes a financial-leakage
   gap of the same shape Phase 7 already fixed for text.
3. **Ollama parallel tool calls in streaming** — official docs confirm streaming tool calls generally but don't
   explicitly confirm multiple concurrent calls in one streamed response. Recommendation: build against the
   documented single/sequential-call shape, and recheck against a live Ollama install at build time before this
   ever demos against real Ollama rather than the mock — same "verify at build time" posture Phase 8 already used
   for Ollama's `format` field. Not a blocker for starting the build.
4. **Mid-stream tool-call cutoff (§5)** — confirm the "client's problem to detect and discard an incomplete
   argument fragment" posture, rather than any gateway-side buffer-the-whole-call-before-forwarding alternative.
   Recommendation: client-side, for the reasons in §5 (defeats the point of streaming otherwise).

---

## 9. Files this phase will touch (once scoped)

`app/core/schema.py` (`ToolCallDelta`, `UnifiedStreamChunk.tool_call_deltas`, remove the streaming+tools
validator) · `app/providers/openai_adapter.py` · `app/providers/anthropic_adapter.py` ·
`app/providers/ollama_adapter.py` (streaming tool-call parsing in each `stream()`) · `app/api/v1_chat.py`
(`_reconcile_and_bill_partial`'s `accumulated_text` extension) · `deploy/mock-providers/main.py` (tool-call
streaming fixtures) · `tests/unit/test_streaming_tool_calls.py` (new) · `tests/unit/test_fallback_chain.py`,
`tests/unit/test_streaming_passthrough.py`, `tests/unit/test_schema_normalization.py` (extended/updated in
place, per §7) · `docs/PHASE8B_IMPLEMENTATION_GUIDE.md` (final delivery guide, written at delivery time per this
project's standing instruction, not in this scoping pass).

**Explicitly not touched:** `app/resilience/fallback.py` (confirmed no logic change needed, §5) ·
`app/providers/gemini_adapter.py` (confirmed no change, §3.4) · `app/observability/tracing.py` (no required
change, §6) · anything org/quota/budget-related (Phase 8's own scope, unaffected by streaming shape).
