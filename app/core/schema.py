"""
Unified request/response schema for the LLM Gateway.

Every provider adapter (app/providers/*) translates to/from this shape.
Route handlers, policy enrichment, and (in later phases) rate limiting and
observability all speak this schema and never see a provider-native payload
directly — that is the entire point of the gateway.

Reference: LLM_Gateway_Project_Documentation.docx, Document 02 (TRD) ->
"Provider protocol normalization" table, and Document 05 (Backend Schema).
"""

from __future__ import annotations

import time
import uuid
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Role = Literal["user", "assistant", "tool"]
Priority = Literal["high", "normal", "batch"]
FinishReason = Literal["stop", "length", "content_filter", "tool_calls", "error"] | None


class ToolDefinition(BaseModel):
    """
    One callable tool a client offers the model, per Phase 8 (Document 06's
    Phase 8 build task 1 / docs/PHASE8_KICKOFF_SCOPING.md §2).

    `parameters` is a JSON Schema object in the OpenAI/Anthropic-native
    (lowercase `type` values) flavor — that is the gateway's canonical
    shape. Gemini's OpenAPI-style uppercase-enum dialect is a translation
    target produced inside app/providers/gemini_adapter.py, never the
    source of truth (see that adapter's `_to_gemini_schema`).

    `strict` forwards verbatim to OpenAI's `strict` / Anthropic's `strict`
    (both mean "guarantee the model's arguments validate against
    `parameters`"). Gemini has no per-tool strict flag — the Gemini
    adapter silently ignores it rather than erroring, logged once, same
    posture as every other unsupported-knob case in this codebase (see
    OllamaAdapter's tool_choice handling).
    """

    name: str = Field(..., min_length=1)
    description: str = ""
    parameters: dict = Field(default_factory=dict)
    strict: bool = False


class ForcedToolChoice(BaseModel):
    """Force the model to call one specific, named tool."""

    name: str = Field(..., min_length=1)


ToolChoiceMode = Literal["auto", "none", "required"]
# UnifiedChatRequest.tool_choice: ToolChoiceMode | ForcedToolChoice | None
# `None` with `tools` present means "auto" — every provider's own default,
# so callers never have to inject one just to get default behavior.


class ToolCall(BaseModel):
    """
    One tool invocation the model asked for, normalized across providers.

    `arguments` is ALWAYS a JSON-encoded string — OpenAI's own native
    shape. Anthropic (`tool_use.input`) and Gemini/Ollama
    (`functionCall.args` / `tool_calls[].function.arguments`) hand back a
    *parsed object* on the wire; each of those three adapters does
    `json.dumps`/`json.loads` at its own translation boundary so nothing
    outside app/providers/*.py ever branches on which provider produced a
    given call.

    `id` is OpenAI's `call_id` / Anthropic's `tool_use.id` passed through
    verbatim. Gemini and Ollama have no call-id concept on the wire at
    all — both adapters synthesize one (`f"call_{index}"`, by
    response-array position) purely for this field's sake, and drop it
    again (matching back by array order, not by id) when building the
    next request's tool-result items. See each adapter's own docstring.
    """

    id: str
    name: str
    arguments: str = "{}"


class ChatMessage(BaseModel):
    """
    One turn in the conversation.

    NOTE: system prompts are deliberately *not* a role here. Anthropic's
    Messages API takes system as a top-level string, not a message in the
    list — modeling the unified schema the same way (system as a top-level
    field on UnifiedChatRequest) means the OpenAI/Ollama adapters have to do
    the work of re-inserting it as messages[0], rather than every caller
    having to know which providers want it where.

    Phase 8: `content` is no longer required-non-empty — a tool-calling
    assistant turn is often *empty* text with the entire payload in
    `tool_calls` (Anthropic's own `content` array for such a turn is
    `[{"type":"tool_use",...}]` with no text block at all). `tool_calls`
    (assistant-only) and `tool_call_id` (tool-only, "which call does this
    answer") are additive — every Phase 1-7 message (role in
    {"user","assistant"}, no tool fields) validates identically to before.
    """

    role: Role
    content: str = ""
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None

    @model_validator(mode="after")
    def _validate_tool_shape(self) -> ChatMessage:
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
    """
    Phase 8 adds `json_schema` — a schema-guaranteed structured output,
    distinct from the looser pre-existing `json_object` ("valid JSON, no
    fixed shape"). `json_schema` is required (and validated) exactly when
    `type == "json_schema"`, same field-gating pattern
    UnifiedChatRequest.stop_max_four already uses for `stop`.

    Provider support (docs/PHASE8_KICKOFF_SCOPING.md §4): OpenAI
    (`text.format.json_schema`) and Gemini (`generationConfig.
    responseSchema`) are unconditional. Anthropic's `output_config.
    format.json_schema` is real but model-family-gated (Sonnet 4.5/Opus
    4.1+ only per current docs) — AnthropicAdapter.call() detects an
    "unsupported" 400 on first attempt and falls back to a synthetic
    forced-tool-call emulation automatically (see that adapter's
    `_STRUCTURED_OUTPUT_FALLBACK_TOOL`), per explicit Phase 8 sign-off:
    "use it directly, but also keep a defensive fallback for unsupported
    models."
    """

    type: Literal["text", "json_object", "json_schema"] = "text"
    json_schema: dict | None = None

    @model_validator(mode="after")
    def _validate_json_schema_present(self) -> ResponseFormat:
        if self.type == "json_schema" and not self.json_schema:
            raise ValueError('response_format.json_schema is required when type == "json_schema"')
        if self.type == "json_schema" and "schema" not in self.json_schema:
            raise ValueError('response_format.json_schema must contain a "schema" key')
        return self


class UnifiedChatRequest(BaseModel):
    """
    The single request shape every client sends to POST /v1/chat/completions,
    regardless of which provider eventually serves it.

    `model` is a provider-qualified id in Phase 1 (e.g. "openai:gpt-5.4",
    "anthropic:claude-sonnet-5", "ollama:llama3.2"). Phase 3 adds an
    abstract-tier layer on top (e.g. "tier-1-reasoning") resolved via
    tiers.yaml with fallback chains — Phase 1 intentionally does not build
    that yet; see app/providers/registry.py.
    """

    model: str = Field(..., min_length=1)
    messages: list[ChatMessage]
    system: str | None = None
    max_tokens: int = Field(default=1024, gt=0, le=128_000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)
    stop: list[str] | None = None
    stream: bool = False
    response_format: ResponseFormat | None = None
    priority: Priority = "normal"
    tools: list[ToolDefinition] | None = None
    tool_choice: ToolChoiceMode | ForcedToolChoice | None = None

    @field_validator("messages")
    @classmethod
    def messages_must_not_be_empty(cls, v: list[ChatMessage]) -> list[ChatMessage]:
        if not v:
            raise ValueError("messages must contain at least one entry")
        return v

    @field_validator("stop")
    @classmethod
    def stop_max_four(cls, v: list[str] | None) -> list[str] | None:
        # OpenAI and Anthropic both cap stop sequences at 4; enforce the
        # tightest common constraint at the gateway boundary so a client
        # never has to learn this per-provider.
        if v is not None and len(v) > 4:
            raise ValueError("stop supports at most 4 sequences")
        return v

    @model_validator(mode="after")
    def _tools_not_yet_supported_with_streaming(self) -> UnifiedChatRequest:
        # Phase 8 ships non-streaming tool calling only (explicit developer
        # sign-off, docs/PHASE8_KICKOFF_SCOPING.md §5/§9 Q1) — every
        # provider's streaming tool-call event shape is different and
        # non-trivial, and silently DROPPING tool-call info out of a
        # stream (rather than erroring) would be worse than refusing the
        # combination outright. Rejected here, once, at the schema
        # boundary — a clean 422 with a self-explanatory reason — rather
        # than as a per-adapter silent gap four different ways.
        if self.stream and self.tools:
            raise ValueError(
                "tools + stream=true is not supported yet (Phase 8 ships non-streaming "
                "tool calling only — see docs/PHASE8_KICKOFF_SCOPING.md §5); retry "
                "without stream, or without tools."
            )
        return self


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class UnifiedChoice(BaseModel):
    index: int = 0
    message: ChatMessage
    finish_reason: FinishReason = None


class UnifiedChatResponse(BaseModel):
    """Normalized non-streaming response, returned to the client as 200 JSON."""

    id: str
    object: Literal["chat.completion"] = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    provider: str
    model_requested: str
    model_served: str
    choices: list[UnifiedChoice]
    usage: Usage

    @classmethod
    def new_id(cls) -> str:
        return f"gw-{uuid.uuid4().hex[:24]}"


class UnifiedStreamChunk(BaseModel):
    """
    One normalized SSE data frame emitted to the client during streaming.

    `usage` is populated only on the terminal chunk once the upstream
    provider has reported final token counts (all three providers report
    usage differently — see each adapter's `stream()`).
    """

    id: str
    object: Literal["chat.completion.chunk"] = "chat.completion.chunk"
    provider: str
    model_served: str
    delta: str = ""
    finish_reason: FinishReason = None
    usage: Usage | None = None
