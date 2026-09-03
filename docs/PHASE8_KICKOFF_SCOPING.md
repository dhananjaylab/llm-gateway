# LLM Gateway — Phase 8: Provider Capability Parity
## Kickoff Scoping & Architecture Document (Tool Calling · Structured Outputs · Hierarchical Quotas)

**Status:** design/scoping only — **no application code shipped in this pass**. Per this project's own working
agreement ("stop for developer sign-off... before starting the next phase," carried through every phase from
Phase 1's TRD Appendix A to Phase 7's own kickoff), this document is that sign-off gate. Build starts once §9's
three questions are answered.

**Baseline verified, not assumed:** cloned `dhananjaylab/llm-gateway` fresh (`git clone
https://github.com/dhananjaylab/llm-gateway`), confirmed `main` is at `eb6bcdf` — PR #24
(`feature/phase7-perf`), merged — and ran the real suite in a clean venv:

```
255 passed, 8 skipped in 19.13s
```

This matches `docs/PHASE7_IMPLEMENTATION_GUIDE.md`'s claimed status exactly, byte-for-byte. Phase 7 is genuinely
done, not just documented as done — this phase builds on a confirmed-green foundation, not the memory-noted
"interrupted mid-session" state (that note is stale; the merged PR shows the `_ConstructionCounter` fix and every
other Phase 7 build task already landed and tested).

**Source:** `docs/PHASE7_PLUS_ADOPTION_PLAN.md` §3 "Phase 8" is the starting scope. This document re-scopes it
against **fresh web research (Aug 22 2026)** on the actual current wire formats of the OpenAI Responses API,
Anthropic Messages API, Gemini `generateContent` API, and Ollama's native `/api/chat` — because the original
plan's own Phase 8 sketch was written from general knowledge, not verified against current docs the way this
project's "search first" convention requires for anything that ships.

---

## 0. Research finding that changes the plan

`PHASE7_PLUS_ADOPTION_PLAN.md` §3, Phase 8 build task 3, said: *"Anthropic has no native strict-schema mode as of
this research pass... document as a known cross-provider capability gap (gateway either 400s clearly... or falls
back to tool-call-based extraction)."*

**That's no longer accurate.** As of this pass, Claude's Messages API has a top-level `output_config: {"format":
{"type": "json_schema", "schema": {...}}}` for guaranteed-schema JSON responses, plus `strict: true` on individual
tool definitions for guaranteed tool-argument adherence — both now sit at the same tier as OpenAI's
`text.format.json_schema`/`strict` and Gemini's `responseSchema`. Independently corroborated via LiteLLM's own
provider docs, which note structured outputs are "fully supported for Claude Sonnet 4.5 and Opus 4.1 models" —
i.e. it's real, shipped, and (like most Anthropic features) rolled out per-model-family, not universally.

**Practical effect:** Phase 8 no longer needs the tool-call-extraction fallback or the "clear 400" escape hatch
the original plan reserved for Anthropic — all three cloud providers get native structured-output translation.
The one thing that *is* still an open question (folded into §9, Question 2) is what the gateway does when the
**pinned** Claude model in `config/tiers.yaml`/`config/pricing.yaml` doesn't support `output_config` — that's a
runtime fact this pass can't verify without a live account, not a documentation gap.

---

## 1. Goal

Close the two real capability gaps `PHASE7_PLUS_ADOPTION_PLAN.md` §2 rows 11–12 identified as genuinely missing
— tool/function calling and structured (schema-guaranteed) outputs — across all **four** adapters (OpenAI,
Anthropic, Gemini, Ollama; the original plan only scoped three), plus — scope pending Question 3 — quota
enforcement above team level (row 13).

---

## 2. Schema evolution — `app/core/schema.py`

Additive only. Every existing Phase 1–7 request (no `tools` field) must produce byte-identical adapter payloads
to today — this is the same non-negotiable this codebase applied to every prior schema change (see `schema.py`'s
own docstring on why `system` isn't a message role).

```python
Role = Literal["user", "assistant", "tool"]        # was ["user", "assistant"]

class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict            # JSON Schema object — OpenAI/Anthropic-native flavor is the
                                 # gateway's canonical shape; Gemini's OpenAPI-subset is a
                                 # translation target, not the source of truth (see §3.3).
    strict: bool = False        # forwarded to OpenAI `strict` / Anthropic `strict` verbatim;
                                 # Gemini has no per-tool strict flag — silently ignored there,
                                 # flagged in that adapter's docstring, not silently dropped.

class ForcedToolChoice(BaseModel):
    name: str

ToolChoiceMode = Literal["auto", "none", "required"]
# UnifiedChatRequest.tool_choice: ToolChoiceMode | ForcedToolChoice | None = None
#   None + tools present -> "auto" (every provider's own default, so the gateway doesn't have
#   to inject one at every call site).

class ToolCall(BaseModel):
    id: str                     # gateway-normalized: OpenAI's call_id / Anthropic's tool_use.id
                                 # passed through verbatim; Gemini gets a synthesized id (see §3.3
                                 # — Gemini's wire format has no call-id concept at all).
    name: str
    arguments: str               # JSON-ENCODED STRING, unconditionally — this is OpenAI's native
                                 # shape. Anthropic (`input`) and Gemini/Ollama (`args`) return a
                                 # parsed object on the wire; every non-OpenAI adapter does
                                 # `json.dumps`/`json.loads` at its own translation boundary so
                                 # nothing outside `app/providers/*.py` ever branches on provider.

class ChatMessage(BaseModel):
    role: Role
    content: str = ""            # was `Field(..., min_length=1)` — a tool-calling assistant turn
                                  # is often *empty* text with all the payload in `tool_calls`
                                  # (e.g. Anthropic's content array is `[{"type":"tool_use",...}]`
                                  # with no text block at all). Emptiness is now valid; a
                                  # model-level validator (below) still rejects a message that is
                                  # empty AND carries nothing else.
    tool_calls: list[ToolCall] | None = None    # assistant-role only
    tool_call_id: str | None = None             # tool-role only — which call this answers

    @model_validator(mode="after")
    def _validate_tool_shape(self):
        if self.role == "tool" and not self.tool_call_id:
            raise ValueError("a tool-role message must set tool_call_id")
        if self.role != "assistant" and self.tool_calls:
            raise ValueError("tool_calls is only valid on assistant messages")
        if self.role != "tool" and self.tool_call_id:
            raise ValueError("tool_call_id is only valid on tool messages")
        if self.role == "assistant" and not self.content and not self.tool_calls:
            raise ValueError("an assistant message needs content, tool_calls, or both")
        if self.role == "user" and not self.content:
            raise ValueError("a user message needs content")
        return self

class ResponseFormat(BaseModel):
    type: Literal["text", "json_object", "json_schema"] = "text"   # + "json_schema"
    json_schema: dict | None = None   # {"name": str, "schema": dict, "strict": bool} — required
                                        # when type == "json_schema", validated by a field_validator
                                        # the same way `stop_max_four` already gates `stop`.

FinishReason = Literal["stop", "length", "content_filter", "tool_calls", "error"] | None
```

`UnifiedChatRequest` gains:
```python
tools: list[ToolDefinition] | None = None
tool_choice: ToolChoiceMode | ForcedToolChoice | None = None
```

`UnifiedStreamChunk` is **unchanged** this phase — see §5 (streaming tool calls are the deferred item).

---

## 3. Per-provider translation — wire formats verified this pass

### 3.1 OpenAI — Responses API (`app/providers/openai_adapter.py`)

Request:
```json
{
  "tools": [{"type": "function", "name": "...", "description": "...",
             "parameters": {...}, "strict": true}],
  "tool_choice": "auto" | "none" | "required" | {"type": "function", "name": "..."}
}
```
Structured output (already partially built — the adapter's `text.format` field exists for `json_object`; add the
`json_schema` variant): `"text": {"format": {"type": "json_schema", "name": ..., "schema": {...}, "strict": true}}`.

History round-trip is the fiddly part: OpenAI's Responses API represents a tool call and its result as **two
separate typed items in the `input` array**, not as message roles the way Anthropic/Ollama do:
- A prior assistant `tool_calls` entry → `{"type": "function_call", "call_id": ..., "name": ..., "arguments": "<json string>"}`
- A prior `tool`-role message → `{"type": "function_call_output", "call_id": tool_call_id, "output": content}`

`translate_request` already builds `input` from `request.messages`; this phase extends that loop to emit these
two item shapes for assistant/tool messages that carry tool data, instead of the current flat
`{"role": m.role, "content": m.content}` for every message.

Response parsing: `output[]` items with `"type": "function_call"` → one `ToolCall` each (`arguments` is already
a string — passthrough, no `json.dumps`). `finish_reason` becomes `"tool_calls"` whenever any such item is
present, overriding the existing `_extract_responses_finish_reason`'s `"stop"` default.

### 3.2 Anthropic — Messages API (`app/providers/anthropic_adapter.py`)

Request:
```json
{
  "tools": [{"name": "...", "description": "...", "input_schema": {...}, "strict": true}],
  "tool_choice": {"type": "auto"} | {"type": "any"} | {"type": "none"} | {"type": "tool", "name": "..."},
  "output_config": {"format": {"type": "json_schema", "schema": {...}}}
}
```
`tool_choice` mapping: unified `"auto"→{"type":"auto"}`, `"none"→{"type":"none"}`, `"required"→{"type":"any"}`
(**not** a literal `"required"` string — confirmed against current Claude Platform docs), `ForcedToolChoice(name=X)
→ {"type":"tool","name":X}`.

**The real structural difference** (this is the one `PHASE7_PLUS_ADOPTION_PLAN.md` already flagged, confirmed
correct): a prior assistant tool call round-trips as a `content` block `{"type":"tool_use","id":call_id,
"name":...,"input": <parsed object>}` — `input` is a JSON **object**, not the string `ToolCall.arguments` is
canonically stored as, so `translate_request` does `json.loads(tool_call.arguments)` here specifically. And
Anthropic has **no tool role at all** — a `tool`-role unified message becomes a content block inside a
**`user`**-role message: `{"role":"user","content":[{"type":"tool_result","tool_use_id":call_id,"content":"..."}]}`.
Consecutive unified `tool` messages (parallel tool calls answered in one batch) must be **coalesced into one
Anthropic user message** with multiple `tool_result` blocks — sending them as separate user messages is a
malformed conversation from Anthropic's point of view.

Response parsing: `content[]` blocks with `"type":"tool_use"` → `ToolCall(id=block["id"], name=block["name"],
arguments=json.dumps(block["input"]))`. `stop_reason == "tool_use"` maps to `finish_reason = "tool_calls"` (new
branch in `_map_stop_reason`).

### 3.3 Gemini — `generateContent` (`app/providers/gemini_adapter.py`)

Request:
```json
{
  "tools": [{"functionDeclarations": [{"name": "...", "description": "...", "parameters": {...}}]}],
  "toolConfig": {"functionCallingConfig": {"mode": "AUTO" | "ANY" | "NONE", "allowedFunctionNames": [...]}},
  "generationConfig": {"responseMimeType": "application/json", "responseSchema": {...}}
}
```
Two real quirks, both need to be handled explicitly (not silently), matching this adapter's existing pattern of
flagging translation gaps in its own docstring (see the current file's `model`-field-leak history):

1. **Schema case convention.** Gemini's `parameters`/`responseSchema` use an OpenAPI-Schema-Object dialect with
   `type` as an *enum of uppercase strings* (`OBJECT`, `STRING`, `NUMBER`, `INTEGER`, `BOOLEAN`, `ARRAY`), not
   standard lowercase JSON Schema. Since the gateway's canonical `ToolDefinition.parameters` /
   `ResponseFormat.json_schema` are OpenAI/Anthropic-flavored (lowercase), this adapter needs one shared,
   recursive `_to_gemini_schema(schema: dict) -> dict` helper that upper-cases every `type` value and passes
   everything else through — reused for both tool parameters and `responseSchema`. JSON Schema keywords Gemini's
   subset doesn't support (`oneOf`, `$ref`, etc.) are **not silently dropped** — the helper raises a clear
   `ProviderError(..., error_type="unsupported_schema_feature")` rather than sending Gemini a schema it will
   either reject or silently ignore.
2. **No call-id concept.** Unlike OpenAI (`call_id`) and Anthropic (`tool_use.id`), Gemini's `functionCall` parts
   carry only `name` + `args` — correlation with the eventual `functionResponse` is positional/name-based, not
   ID-based (Google's own Gemini-3 docs describe an internal cryptographic *signature* field for parallel-call
   ordering, which is a different, opaque mechanism the gateway doesn't need to touch as long as response order
   is preserved). To keep `ToolCall.id` non-optional across all four providers (so client code never has to
   branch on provider), this adapter **synthesizes** an id (`f"call_{index}"` by response-array position) purely
   for gateway-internal/client-facing bookkeeping, and **drops it** when building the next request's
   `functionResponse` parts — matched back to the right call by preserving array order, not by echoing the
   synthetic id anywhere Gemini would see it. This is a documented, tested asymmetry, not a bug.

Response parsing: `candidates[0].content.parts[]` — parts with a `functionCall` key → `ToolCall(id=synthesized,
name=part["functionCall"]["name"], arguments=json.dumps(part["functionCall"]["args"]))`.

### 3.4 Ollama — native `/api/chat` (`app/providers/ollama_adapter.py`)

Already OpenAI-*tools*-shaped on the request side (confirmed current: `tools: [{"type":"function","function":
{"name","description","parameters"}}]`, nested one level deeper than OpenAI's own Responses-API-flat shape).
`tool_choice` has **no Ollama equivalent** — there is no way to force/forbid a tool call; a non-`None`
`tool_choice` on an Ollama-routed request is accepted but has no wire effect, logged once at translate time
(matches this codebase's existing "log loudly, don't crash on an unsupported knob" posture — see Gemini's
`strict` handling in §3.3).

Response: `message.tool_calls: [{"function": {"name": ..., "arguments": {<parsed object>}}}]` → `ToolCall(id=
f"call_{index}"` (Ollama also has no call-id — same synthesis approach as Gemini), `arguments=json.dumps(...)`.
Tool results round-trip as `{"role": "tool", "content": ...}` messages — this is the one provider whose wire
shape for tool results maps directly onto the unified `tool` role with **zero** structural translation, which is
a useful cross-check when writing `test_tool_calling_translation.py`.

**Model support is silent and per-model.** Whether a given local model's chat template supports tool calling at
all is not knowable from the gateway side ahead of time — an unsupported model just answers in prose and
`tool_calls` is absent from the response, which the gateway already handles correctly as "no tool call happened"
(not an error). Worth one line in the adapter's docstring so a future reader doesn't mistake this for a bug.

---

## 4. Structured outputs — translation table

| Provider | Request field | Model gating |
|---|---|---|
| OpenAI | `text.format = {"type":"json_schema","name":...,"schema":...,"strict":true}` | GA across current GPT-5.x |
| Anthropic | `output_config.format = {"type":"json_schema","schema":...}` | **Claude Sonnet 4.5 / Opus 4.1 and later only** (per current docs) — see §9 Q2 |
| Gemini | `generationConfig.{responseMimeType:"application/json", responseSchema:...}` | GA across current Gemini 3.x |
| Ollama | native `format` field already accepts a raw JSON-schema-ish object per Ollama's own structured-output support — **not yet verified against a live install this pass**, flagged for a build-time recheck rather than assumed |

`ResponseFormat(type="json_schema")` is the one new unified value; `json_object` (existing) keeps mapping to each
adapter's loose "any valid JSON" mode unchanged.

---

## 5. Explicitly deferred out of this phase

**Streaming tool calls.** All three cloud providers' streaming tool-call event shapes are meaningfully different
and non-trivial on their own: OpenAI emits incremental `response.function_call_arguments.delta` events; Anthropic
streams `content_block_start`/`content_block_delta` (`partial_json` deltas) /`content_block_stop` around a
`tool_use` block; Gemini's `functionCall` parts arrive whole, not token-by-token. Supporting this would also mean
extending `UnifiedStreamChunk` with a `tool_call_delta` field, which ripples into `app/resilience/fallback.py`'s
mid-stream fallback-cutoff logic (§ its own docstring: fallback is scoped to "before any content chunk" — a
partial tool-call-in-progress needs the same treatment, worked out carefully, not bolted on). Recommend: **ship
non-streaming tool calling in Phase 8**, exactly the same staged approach this project already used for Gemini
streaming in Phase 1 (`GeminiAdapter.stream()` raises a structured `ProviderError` rather than half-implementing
it) — streaming tool calls become an explicitly named Phase 8b or Phase 9 stretch item, not a silent gap.

**MCP-native tool declarations.** Gemini gained native MCP support in March 2026 (auto-generates
`functionDeclarations` from an MCP session) — interesting, but a different feature (dynamic tool discovery vs.
this phase's static tool-schema translation) and out of scope here.

---

## 6. Hierarchical quotas — three shapes, pick one (Question 3)

| | **A. Org-level only** | **B. Full org→team→app→user** | **C. Defer entirely** |
|---|---|---|---|
| New concepts | `org_id` on `TeamConfig` (default `"default-org"`, backward-compatible), `OrgConfig`, `OrgConfigStore` mirroring `TeamConfigStore` verbatim | Above, plus `app_id`/`user_id` — concepts that don't exist anywhere in this codebase today (no auth header, no config schema, no admin surface) | — |
| Lua/Redis impact | `combined_check.lua` gains one more RPM/TPM pair + one more budget key (9 KEYS total, still one EVALSHA) | 4 axes × 2 buckets + budget = up to 9+ KEYS, right at the edge of comfortable single-script scope; Redis Cluster hash-tag co-location (already a noted Document 05 concern) gets harder with more key families | none |
| New Admin API | `GET/PATCH /admin/orgs/{org_id}` (limits + budget), mirroring `/admin/limits`/`/admin/budgets` exactly | Above ×2 more levels, plus a new auth concept for how a sub-team caller identifies its app/user | none |
| Rough size | ~1 day, reuses every existing pattern | Its own phase, not a Phase 8 build task | 0 |
| Risk | Low — pure extension of proven patterns | Real design risk (auth model change) rushed inside an already-large phase | Leaves Doc 1 row 13 open |

Recommendation: **A**, if hierarchical quotas ship this phase at all — B is large enough, and touches
authentication (a different subsystem than anything else in Phase 8), that it deserves to be scoped as its own
phase rather than a build task inside this one.

---

## 7. Test plan (files this phase adds/extends)

| File | Proves |
|---|---|
| `tests/unit/test_tool_calling_translation.py` (new) | A shared tool definition translates correctly to all four providers' request shapes (parameters/input_schema/functionDeclarations/tools); each provider's distinct tool-call response shape normalizes back to one `ToolCall` list; the full round trip (request → provider tool call → gateway normalizes → client sends `tool` message → gateway re-translates into history) for at least one provider per adapter family (OpenAI item-pair, Anthropic content-block, Gemini positional, Ollama role-based) |
| `tests/unit/test_structured_outputs.py` (new) | `json_schema` response format translates correctly for OpenAI/Anthropic/Gemini; Ollama's `format` passthrough (pending the build-time recheck in §4); a documented, tested error (not silent degradation) when a request asks for `json_schema` against a Claude model/tier not known to support `output_config` |
| `tests/unit/test_hierarchical_quotas.py` (new, only if Option A/B chosen) | An org-level cap independently denies even when the team-level bucket has headroom; org+team decrements happen atomically together (no partial-consumption race, same atomicity bar as `test_token_bucket_concurrency.py`) |
| `deploy/mock-providers/main.py` (extended) | Optionally emits tool-call-shaped responses, request-flag-gated (e.g. a reserved tool name or header) so all 10 existing wire-compat tests need zero changes |
| Regression | Full 255+8 baseline re-run green; existing schema/adapter tests (`test_schema_normalization.py`, `test_response_normalization.py`) get a small number of additive cases, not rewrites — `tools`/`tool_choice` absent must still produce today's exact payloads |

---

## 8. Files this phase will touch once scoped (for the "deliver only changed files" pass)

`app/core/schema.py` · `app/providers/{openai,anthropic,gemini,ollama}_adapter.py` · `app/api/v1_chat.py` (pass
`tools`/`tool_choice`/structured `response_format` through; handle `finish_reason="tool_calls"`) ·
`deploy/mock-providers/main.py` · new test files above · `docs/PHASE8_IMPLEMENTATION_GUIDE.md` (final delivery
guide, per this project's standing instruction) — plus, only if Option A/B: `app/core/config.py`,
`app/core/team_store.py`, `app/api/admin.py`, `app/ratelimit/combined.py` + `.lua`, `config/orgs.yaml`.

---

## 9. Open decisions requiring sign-off before build starts

1. **Streaming tool calls this phase or deferred?** (§5 lays out why this roughly doubles the phase's real
   complexity — different event shapes per provider, plus a `UnifiedStreamChunk`/fallback-router schema change.)
2. **Anthropic structured-output/strict-tool gating**, now that native support exists (§0): use it directly and
   document the model-family requirement, or also keep a defensive fallback/clear-error path for a pinned model
   that doesn't support it?
3. **Hierarchical quotas** — Option A (org-level, ~1 day, this phase), Option B (full tree, its own phase), or
   Option C (defer, keep Phase 8 focused purely on tool calling + structured outputs)?
