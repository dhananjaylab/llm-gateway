"""
OpenAI adapter (Chat Completions API).

VERSION NOTE (verify at build time, per the TRD working agreement): OpenAI's
current-generation chat models (the GPT-5.x family) require
`max_completion_tokens` rather than the legacy `max_tokens` field, and
support `stream_options: {"include_usage": true}` to get a final usage
frame on a streamed response instead of having to locally re-tokenize.
Both are applied below. GPT-5.x also rejects the `stop` parameter outright
(400 "Unsupported parameter: 'stop' is not supported with this model") —
confirmed for gpt-5.4, the model this gateway currently serves — so it is
never forwarded (see the comment in translate_request). Re-verify field
names against https://platform.openai.com/docs/api-reference/chat before
pinning a model family in production, since this is the adapter most
likely to drift.

UPDATE (Phase 4, model-roster upgrade to GPT-5.6 Sol/Terra): confirmed
against current OpenAI/Azure OpenAI docs that GPT-5.6 and later models
also don't support `temperature` or `top_p` on Chat Completions — the
same "modern reasoning-family" constraint that already made `stop`
unsendable, just wider. This mirrors the constraint
anthropic_adapter.py already applies to Sonnet 5/Opus 4.7+ (see that
file's translate_request). Both are now omitted unconditionally, same
reasoning as `stop`: every OpenAI model currently configured in
config/tiers.yaml / config/teams.yaml is GPT-5.x, so there's no
still-supported model in this adapter's current scope that would need
them forwarded. If an older model (e.g. gpt-4o) re-enters scope, this
needs to become conditional on provider_model, same flag as `stop`'s.

Endpoint: POST {base_url}/v1/chat/completions
Auth: Authorization: Bearer <OPENAI_API_KEY>
Streaming: SSE, "data: {json}\\n\\n" frames, terminated by "data: [DONE]".
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator

import httpx

from app.core.schema import (
    ChatMessage,
    UnifiedChatRequest,
    UnifiedChatResponse,
    UnifiedChoice,
    UnifiedStreamChunk,
    Usage,
)
from app.providers.base import ProviderAdapter, ProviderError

# HTTP statuses the TRD classifies as transient/retryable vs. permanent.
# Phase 1 only uses this to label ProviderError; Phase 3 acts on it.
_RETRYABLE_STATUS = {429, 502, 503, 504}


class OpenAIAdapter(ProviderAdapter):
    provider_name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    # -- request translation -------------------------------------------------

    def translate_request(self, request: UnifiedChatRequest, *, provider_model: str) -> dict:
        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend({"role": m.role, "content": m.content} for m in request.messages)

        payload: dict = {
            "model": provider_model,
            "messages": messages,
            "max_completion_tokens": request.max_tokens,
            "stream": request.stream,
        }
        # NOTE: GPT-5.x models (gpt-5.4 previously, gpt-5.6-sol/-terra as
        # of the Phase 4 roster upgrade — every OpenAI model currently
        # configured in config/tiers.yaml / config/teams.yaml) reject
        # `stop`, `temperature`, and `top_p` outright with a 400
        # "Unsupported parameter" error — confirmed against current
        # OpenAI/Azure OpenAI docs for the GPT-5.x family, not a
        # hypothetical. This is now the SAME class of constraint
        # Anthropic's adapter already applies to Sonnet 5/Opus 4.7+ (see
        # anthropic_adapter.py) — a provider-wide-for-this-model-family
        # omission, not a per-request choice by the caller. Phase 1's
        # adapter has no per-served-model conditional logic, and every
        # OpenAI model currently in scope is GPT-5.x, so all three are
        # deliberately never forwarded here. If an older model (e.g.
        # gpt-4o) is added to this adapter's scope later, this needs to
        # become conditional on provider_model rather than blanket-omitted.
        if request.response_format is not None:
            payload["response_format"] = {"type": request.response_format.type}
        if request.stream:
            # Ask for a terminal usage frame instead of locally re-tokenizing
            # the assembled completion (see TRD streaming section).
            payload["stream_options"] = {"include_usage": True}
        return payload

    # -- non-streaming call ---------------------------------------------------

    async def call(self, payload: dict) -> dict:
        url = f"{self._base_url}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "openai request timed out", retryable=True, error_type="timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"openai transport error: {exc}", retryable=True, error_type="transport_error"
            ) from exc

        if resp.status_code >= 400:
            _raise_for_status(resp)
        return resp.json()

    # -- response translation -------------------------------------------------

    def translate_response(
        self, raw: dict, *, request: UnifiedChatRequest, provider_model: str
    ) -> UnifiedChatResponse:
        choice = raw["choices"][0]
        usage_raw = raw.get("usage") or {}
        usage = Usage(
            input_tokens=usage_raw.get("prompt_tokens", 0),
            output_tokens=usage_raw.get("completion_tokens", 0),
            cache_read_input_tokens=(usage_raw.get("prompt_tokens_details") or {}).get(
                "cached_tokens", 0
            ),
        )
        return UnifiedChatResponse(
            id=raw.get("id", UnifiedChatResponse.new_id()),
            created=raw.get("created", int(time.time())),
            provider=self.provider_name,
            model_requested=request.model,
            model_served=raw.get("model", provider_model),
            choices=[
                UnifiedChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant", content=choice["message"]["content"] or ""
                    ),
                    finish_reason=_map_finish_reason(choice.get("finish_reason")),
                )
            ],
            usage=usage,
        )

    # -- streaming --------------------------------------------------------------

    async def stream(
        self, payload: dict, *, request: UnifiedChatRequest, provider_model: str
    ) -> AsyncIterator[UnifiedStreamChunk]:
        url = f"{self._base_url}/v1/chat/completions"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        chunk_id = f"gw-{uuid.uuid4().hex[:24]}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream("POST", url, json=payload, headers=headers) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        _raise_for_status_bytes(resp.status_code, body)

                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[len("data:") :].strip()
                        if data == "[DONE]":
                            break

                        event = json.loads(data)
                        served_model = event.get("model", provider_model)

                        # The terminal usage-only frame (stream_options) has
                        # an empty choices list.
                        if not event.get("choices"):
                            usage_raw = event.get("usage")
                            if usage_raw:
                                yield UnifiedStreamChunk(
                                    id=chunk_id,
                                    provider=self.provider_name,
                                    model_served=served_model,
                                    delta="",
                                    usage=Usage(
                                        input_tokens=usage_raw.get("prompt_tokens", 0),
                                        output_tokens=usage_raw.get("completion_tokens", 0),
                                    ),
                                )
                            continue

                        choice = event["choices"][0]
                        delta_text = (choice.get("delta") or {}).get("content") or ""
                        finish_reason = _map_finish_reason(choice.get("finish_reason"))
                        yield UnifiedStreamChunk(
                            id=chunk_id,
                            provider=self.provider_name,
                            model_served=served_model,
                            delta=delta_text,
                            finish_reason=finish_reason,
                        )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "openai stream timed out", retryable=True, error_type="timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"openai stream transport error: {exc}",
                retryable=True,
                error_type="transport_error",
            ) from exc


def _map_finish_reason(reason: str | None) -> str | None:
    return {
        "stop": "stop",
        "length": "length",
        "content_filter": "content_filter",
    }.get(reason or "", reason)


def _raise_for_status(resp: httpx.Response) -> None:
    _raise_for_status_bytes(resp.status_code, resp.content)


def _raise_for_status_bytes(status_code: int, body: bytes) -> None:
    try:
        detail = json.loads(body).get("error", {}).get("message", body.decode(errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        detail = body.decode(errors="replace")
    error_type = "auth_error" if status_code in (401, 403) else "openai_error"
    if status_code == 429:
        error_type = "rate_limit_exceeded"
    raise ProviderError(
        f"openai returned {status_code}: {detail}",
        status_code=status_code,
        retryable=status_code in _RETRYABLE_STATUS,
        error_type=error_type,
    )
