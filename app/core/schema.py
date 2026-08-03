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

from pydantic import BaseModel, Field, field_validator

Role = Literal["user", "assistant"]
Priority = Literal["high", "normal", "batch"]
FinishReason = Literal["stop", "length", "content_filter", "error"] | None


class ChatMessage(BaseModel):
    """
    One turn in the conversation.

    NOTE: system prompts are deliberately *not* a role here. Anthropic's
    Messages API takes system as a top-level string, not a message in the
    list — modeling the unified schema the same way (system as a top-level
    field on UnifiedChatRequest) means the OpenAI/Ollama adapters have to do
    the work of re-inserting it as messages[0], rather than every caller
    having to know which providers want it where.
    """

    role: Role
    content: str = Field(..., min_length=1)


class ResponseFormat(BaseModel):
    type: Literal["text", "json_object"] = "text"


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
