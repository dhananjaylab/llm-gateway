"""
Anthropic adapter (Messages API).

This is the adapter the TRD's implementation guide says to build second,
specifically because it "forces you to handle the system-prompt-as-top-
level-field difference": unlike OpenAI/Ollama, Anthropic never accepts a
system message inside `messages` — it is always the separate top-level
`system` string.

Endpoint: POST {base_url}/v1/messages
Auth: x-api-key: <ANTHROPIC_API_KEY>, anthropic-version: 2023-06-01
Streaming: SSE with typed events (message_start, content_block_delta,
message_delta, message_stop, ...) rather than OpenAI's single repeated
"chat.completion.chunk" shape — each event type carries a different
payload, which is the other edge case this adapter exists to surface.

VERSION NOTE: anthropic-version is a stable, explicitly-pinned API version
string (not a model version) and has not changed since the Messages API
shipped; re-verify at https://docs.claude.com/en/api/versioning if adapter
calls start returning a version-related 4xx.
"""

from __future__ import annotations

import json
import time
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

_ANTHROPIC_VERSION = "2023-06-01"
_RETRYABLE_STATUS = {429, 502, 503, 504}


class AnthropicAdapter(ProviderAdapter):
    provider_name = "anthropic"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def _headers(self) -> dict:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    # -- request translation -------------------------------------------------

    def translate_request(self, request: UnifiedChatRequest, *, provider_model: str) -> dict:
        payload: dict = {
            "model": provider_model,
            "max_tokens": request.max_tokens,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": request.stream,
        }
        if request.system:
            payload["system"] = request.system
        # NOTE: Claude Sonnet 5, Opus 4.7+ have completely removed support for
        # temperature, top_p, and top_k sampling parameters. Setting them to any
        # value (including non-default) returns a 400 error. The docs explicitly
        # state: "Remove these parameters when migrating; the default value (or
        # omitting the parameter) is accepted."
        # We omit these parameters entirely to maintain compatibility with newer
        # models. For older models that still support them, clients should use
        # system prompt instructions instead.
        if request.stop is not None and request.stop:
            payload["stop_sequences"] = request.stop
        return payload

    # -- non-streaming call ---------------------------------------------------

    async def call(self, payload: dict) -> dict:
        url = f"{self._base_url}/v1/messages"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "anthropic request timed out", retryable=True, error_type="timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"anthropic transport error: {exc}", retryable=True, error_type="transport_error"
            ) from exc

        if resp.status_code >= 400:
            _raise_for_status(resp)
        return resp.json()

    # -- response translation -------------------------------------------------

    def translate_response(
        self, raw: dict, *, request: UnifiedChatRequest, provider_model: str
    ) -> UnifiedChatResponse:
        text_blocks = [b["text"] for b in raw.get("content", []) if b.get("type") == "text"]
        usage_raw = raw.get("usage") or {}
        usage = Usage(
            input_tokens=usage_raw.get("input_tokens", 0),
            output_tokens=usage_raw.get("output_tokens", 0),
            cache_read_input_tokens=usage_raw.get("cache_read_input_tokens", 0),
            cache_creation_input_tokens=usage_raw.get("cache_creation_input_tokens", 0),
        )
        return UnifiedChatResponse(
            id=raw.get("id", UnifiedChatResponse.new_id()),
            created=int(time.time()),
            provider=self.provider_name,
            model_requested=request.model,
            model_served=raw.get("model", provider_model),
            choices=[
                UnifiedChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content="".join(text_blocks)),
                    finish_reason=_map_stop_reason(raw.get("stop_reason")),
                )
            ],
            usage=usage,
        )

    # -- streaming --------------------------------------------------------------

    async def stream(
        self, payload: dict, *, request: UnifiedChatRequest, provider_model: str
    ) -> AsyncIterator[UnifiedStreamChunk]:
        url = f"{self._base_url}/v1/messages"
        message_id = None
        served_model = provider_model
        input_tokens = 0

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "POST", url, json=payload, headers=self._headers()
                ) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        _raise_for_status_bytes(resp.status_code, body)

                    event_type = None
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if line.startswith("event:"):
                            event_type = line[len("event:") :].strip()
                            continue
                        if not line.startswith("data:"):
                            continue

                        data = json.loads(line[len("data:") :].strip())

                        if event_type == "message_start":
                            message = data.get("message", {})
                            message_id = message.get("id", message_id)
                            served_model = message.get("model", served_model)
                            input_tokens = (message.get("usage") or {}).get("input_tokens", 0)

                        elif event_type == "content_block_delta":
                            delta = data.get("delta", {})
                            if delta.get("type") == "text_delta":
                                yield UnifiedStreamChunk(
                                    id=message_id or UnifiedChatResponse.new_id(),
                                    provider=self.provider_name,
                                    model_served=served_model,
                                    delta=delta.get("text", ""),
                                )

                        elif event_type == "message_delta":
                            stop_reason = (data.get("delta") or {}).get("stop_reason")
                            output_tokens = (data.get("usage") or {}).get("output_tokens", 0)
                            yield UnifiedStreamChunk(
                                id=message_id or UnifiedChatResponse.new_id(),
                                provider=self.provider_name,
                                model_served=served_model,
                                delta="",
                                finish_reason=_map_stop_reason(stop_reason),
                                usage=Usage(
                                    input_tokens=input_tokens, output_tokens=output_tokens
                                ),
                            )

                        elif event_type == "message_stop":
                            break
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "anthropic stream timed out", retryable=True, error_type="timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"anthropic stream transport error: {exc}",
                retryable=True,
                error_type="transport_error",
            ) from exc


def _map_stop_reason(reason: str | None) -> str | None:
    return {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
    }.get(reason or "", reason)


def _raise_for_status(resp: httpx.Response) -> None:
    _raise_for_status_bytes(resp.status_code, resp.content)


def _raise_for_status_bytes(status_code: int, body: bytes) -> None:
    try:
        detail = json.loads(body).get("error", {}).get("message", body.decode(errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        detail = body.decode(errors="replace")
    error_type = "auth_error" if status_code in (401, 403) else "anthropic_error"
    if status_code == 429:
        error_type = "rate_limit_exceeded"
    raise ProviderError(
        f"anthropic returned {status_code}: {detail}",
        status_code=status_code,
        retryable=status_code in _RETRYABLE_STATUS,
        error_type=error_type,
    )
