# LLM Gateway — Phase 8: Provider Capability Parity
## Concise Implementation Guide

**Status:** built, tested, passing (**328 passed, 8 skipped** — up from Phase 7's 255/8; regression-clean,
zero flakiness across 3 consecutive full-suite runs).
**Source:** `docs/PHASE7_PLUS_ADOPTION_PLAN.md` §3 Phase 8, re-scoped via fresh web research against current
(Aug 2026) provider docs — see `docs/PHASE8_KICKOFF_SCOPING.md` for the full design/research writeup and the
three explicit developer sign-off decisions this delivery implements verbatim:

1. **Non-streaming tool calling only** this phase — streaming tool calls deferred to 8b/Phase 9.
2. **Anthropic structured outputs used directly, with a defensive fallback** for models that don't support
   `output_config` (real, but rolled out per-model-family, not universal).
3. **Hierarchical quotas: Option A, org-level only** — not the full org→team→app→user tree.

**Baseline verified before any code was written, not assumed:** cloned `dhananjaylab/llm-gateway` fresh,
confirmed `main` was Phase 7's merged state, ran the real suite (255 passed / 8 skipped, matching
`docs/PHASE7_IMPLEMENTATION_GUIDE.md` exactly) before touching anything.

---

## Build tasks — what shipped

### 1. Schema evolution (`app/core/schema.py`)

Additive only — every Phase 1-7 request still produces byte-identical adapter payloads. New: `ToolDefinition`,
`ToolCall`, `ForcedToolChoice`, `ToolChoiceMode`; `Role` gains `"tool"`; `ChatMessage.content` is no longer
required-non-empty (a tool-calling assistant turn can be pure `tool_calls` with no text) but a new
`model_validator` enforces the tool-shape invariants (`tool_call_id` required on `tool`-role messages, forbidden
elsewhere; `tool_calls` assistant-only; an assistant message needs content, tool_calls, or both); `ResponseFormat`
gains `"json_schema"` with its own required-field validator; `FinishReason` gains `"tool_calls"`.

**A second schema-level guard closes the streaming scope decision cleanly, once:** a new `model_validator` on
`UnifiedChatRequest` rejects `tools` + `stream=true` with a self-explanatory 422 — centralizing "tool-call
streaming isn't supported yet" as one clean validation error rather than four different silent per-adapter gaps.

### 2. All four adapters — tool calling + structured outputs

| Provider | Tool round-trip shape | Structured output |
|---|---|---|
| OpenAI (`openai_adapter.py`) | Two typed `input` items (`function_call`/`function_call_output`), not message roles | `text.format.json_schema` |
| Anthropic (`anthropic_adapter.py`) | No tool role — `tool_result` blocks coalesce into `user`-role messages; `input` is an object, not a string | `output_config.format.json_schema`, **with automatic capability fallback** (see below) |
| Gemini (`gemini_adapter.py`) | Uppercase OpenAPI-schema dialect (`_to_gemini_schema`); call ids are optional — synthesized (`gwsyn_{index}` prefix) when absent, dropped (not echoed) on the next request | `responseSchema` + `responseMimeType` |
| Ollama (`ollama_adapter.py`) | Already OpenAI-tools-shaped; the unified `tool` role IS its wire shape verbatim — zero structural translation | native `format` field (schema object or legacy `"json"`) — **not independently re-verified against a live install this pass**, flagged for a build-time recheck |

**Anthropic's structured-output capability fallback** (`AnthropicAdapter.call()`): attempts `output_config`
directly; on a 400 whose error message specifically names `output_config` as unsupported, transparently retries
**once** with the schema re-expressed as a single forced tool call (`__gateway_structured_output__`) — a
technique that works on any tool-calling-capable Claude model regardless of `output_config` support.
`translate_response` unwraps the sentinel tool's `input` back into plain text content, so the client never sees
that a fallback ran and never gets a leaked `ToolCall` for a tool it didn't define. A genuinely unrelated 400
(bad schema, missing `max_tokens`, etc.) is **not** routed through the fallback — only the specific
"unsupported" signal triggers it.

### 3. Real bug found and fixed: `apply_policy`'s PII redaction was dropping tool data

`app/core/policy.py`'s redaction path used to rebuild every message as `ChatMessage(role=m.role,
content=redact_pii(m.content))`, silently correct through Phase 7 (no other fields existed) but **would have
crashed every tool-calling request for any team with `pii_redaction: true`** — the rebuild dropped `tool_calls`
and `tool_call_id`, which fails `ChatMessage`'s own new model_validator. Caught by testing the actual pipeline
before declaring the schema change safe, not assumed. Fixed to preserve both fields; `content` is still redacted
normally. Regression-guarded implicitly by `test_policy_injection.py`'s existing suite plus manual verification
during this delivery (tool_calls/tool_call_id survive redaction; content is still redacted).

### 4. Hierarchical quotas — Option A (org-level only)

New: `OrgConfig`/`load_orgs_config()` (`app/core/config.py`), `config/orgs.yaml` (bootstrap seed, mirrors
`teams.yaml`'s contract exactly), `OrgConfigStore` (`app/core/org_store.py`, mirrors `TeamConfigStore` — Redis is
the runtime source of truth, hot-reload via `gateway:orgconfig:changed` pub/sub). `TeamConfig` gains
`org_id: str = "default-org"` (fully backward compatible; every existing seeded team gets the default with no
`teams.yaml` edit required).

`RateLimiter` and `BudgetEnforcer` gained `check_org`/`peek_org`/`reconcile_org` and
`precheck_org`/`record_spend_org` — **both reuse their existing team-level Lua scripts (`token_bucket.lua`,
`budget_increment.lua`) completely unchanged**, just re-keyed under an `rl:org:{org_id}:...`/`budget:org:{org_id}:...`
namespace that can never collide with a team's own keys. `RateLimiter.check`/`peek`/`reconcile` were refactored
into thin wrappers around a new generic `_check_bucket`/`_peek_bucket`/`_reconcile_bucket` core, shared by the
org path — the team-level call sites and their exact tested behavior are unchanged.

`app/api/v1_chat.py`: an org-level budget + rate check runs **before** the existing (Phase 7-optimized)
combined team-level check — an org-wide block must never let a request reach, and partially consume, team-level
capacity it was never going to be allowed to use. This costs a second Redis round trip on top of Phase 7's
single combined one; a deliberate, documented trade-off for Option A's "~1 day, reuse everything" scope, not a
regression in the team-level path's own tested guarantee. A successful request reconciles/bills **both** levels;
every non-streaming error path and the streaming `_reconcile_and_bill_partial` helper thread `org` through so a
failed/partial request refunds both ledgers too.

New Admin API: `GET/PATCH /admin/orgs/{org_id}` — one combined endpoint (not team's split `/limits` +
`/budgets` pair), a deliberate simplification flagged explicitly in `admin.py`'s own docstring, not a silent
divergence from the established pattern. Same underlying mechanics (hot-reload, audit logging via the existing
`AuditLog`, action `"patch_org"`).

**Known, documented limitation:** an org-level rate-limit denial reuses the existing
`gen_ai_rate_limit_applied_total{team_id, limit_type}` counter with `limit_type="org_rpm"/"org_tpm"` (no metrics
schema change needed); an org-level budget denial reuses `gen_ai_budget_applied_total{team_id}` with **no**
level distinction — a future phase could add a `level` label if that granularity is needed on the Business
dashboard. The HTTP response body itself always carries `"level": "org"` regardless, so client-side handling
never loses the distinction even though the Prometheus counter currently does.

---

## What's explicitly *not* in this phase

Streaming tool calls (§9 Q1) · the full org→team→app→user quota tree (§9 Q3, Option B) · MCP-native tool
declarations · `deploy/mock-providers/main.py` tool-call-shaped mock responses (the integration-test-only surface
this would feed needs a live Docker stack, which this sandbox doesn't have — see Phase 5's README's own "Known
limitation" note; flagged as a follow-up, not silently skipped) · README.md's top section (still titled around
Phase 5/7 content — same deferred-not-forgotten item Phase 7 itself flagged and left for a dedicated pass).

---

## New/changed files (this delivery only)

```
app/core/schema.py                          # ToolDefinition, ToolCall, ForcedToolChoice, ToolChoiceMode,
                                              #   tool role, json_schema response format, streaming+tools guard
app/core/config.py                           # OrgConfig, load_orgs_config, TeamConfig.org_id, orgs_path setting
app/core/org_store.py                        # NEW — OrgConfigStore
app/core/team_store.py                       # persist/deserialize org_id
app/core/policy.py                           # BUGFIX — preserve tool_calls/tool_call_id through PII redaction
app/providers/openai_adapter.py              # tool calling + json_schema structured output
app/providers/anthropic_adapter.py           # tool calling + json_schema (native + capability fallback)
app/providers/gemini_adapter.py              # tool calling + json_schema (schema case-conversion)
app/providers/ollama_adapter.py              # tool calling + json_schema/json_object
app/ratelimit/limiter.py                     # generic bucket core; check_org/peek_org/reconcile_org
app/ratelimit/budget.py                      # period_key_for_org; precheck_org/record_spend_org
app/api/v1_chat.py                           # org-level check wired into the request pipeline
app/api/admin.py                             # GET/PATCH /admin/orgs/{org_id}
app/main.py                                  # org_store wiring, seeding, hot-reload listener
config/orgs.yaml                             # NEW — org bootstrap seed
.env.example                                 # +ORGS_CONFIG_PATH

tests/unit/test_tool_calling_translation.py  # NEW — 30 tests
tests/unit/test_structured_outputs.py        # NEW — 20 tests
tests/unit/test_hierarchical_quotas.py       # NEW — 23 tests

docs/PHASE8_KICKOFF_SCOPING.md               # design/research doc (pre-existing from kickoff)
docs/PHASE8_IMPLEMENTATION_GUIDE.md          # this file
```

No file outside this list was modified. Full regression: **328 passed, 8 skipped**, zero flakiness across 3
consecutive runs.

---

## Done criteria — status

- [x] All pre-Phase-8 tests pass unchanged in behavior (255/8 baseline fully intact within the new 328/8 total)
- [x] A shared tool definition translates correctly to all four providers' request shapes, verified directly
      against each adapter (`test_tool_calling_translation.py`)
- [x] Each provider's distinct tool-call response shape normalizes back to one `ToolCall` list; a full
      request→tool-call→client-answer→re-translated-history round trip is tested for every provider, including
      Anthropic's message-role coalescing and Gemini's real-vs-synthesized id handling
- [x] `json_schema` structured output translates correctly for OpenAI/Anthropic/Gemini/Ollama; Anthropic's
      capability fallback is proven end-to-end (native path, fallback path, and the "don't fall back on an
      unrelated 400" guard) via mocked HTTP, not just unit-level logic
- [x] An org-level cap independently denies even when the team-level bucket has full headroom, and vice versa —
      proven through the real HTTP pipeline, not just at the `RateLimiter`/`BudgetEnforcer` level
- [x] Org+team reconciliation and billing are atomic-per-level and both fire on every success/error/partial-stream
      path
- [x] `ruff check` clean on every file this phase touched

## Open items for developer sign-off before Phase 8b/9

1. **Streaming tool calls.** Scoped out this phase (§9 Q1) — worth confirming whether this becomes "Phase 8b"
   (a focused follow-up) or folds into whatever Phase 9 ends up being, given `PHASE7_PLUS_ADOPTION_PLAN.md`'s
   original Phase 9 was semantic caching + tiers/pricing hot-reload, not streaming tool calls.
2. **Ollama's `format` field for `json_schema`** was not independently re-verified against a live install this
   pass (docs/PHASE8_KICKOFF_SCOPING.md §4) — worth a quick confirmation before this is ever demoed against real
   Ollama rather than the mock/test doubles.
3. **`deploy/mock-providers/main.py` tool-call mock responses** are still unbuilt — needed before
   `tests/integration/` can exercise tool calling against the real containerized stack (this sandbox has no
   Docker daemon to build/verify that against, same known limitation Phase 5's README already documents).
