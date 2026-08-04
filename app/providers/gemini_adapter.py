"""
Gemini adapter (Google Generative AI API).

Endpoint: POST {base_url}/v1beta2/models/{model}:generateContent
Auth: x-goog-api-key: <GEMINI_API_KEY>
Streaming: SSE-like chunked responses from the Google API are not yet
implemented in this Phase 1 integration; the adapter supports non-streaming
calls and a basic streaming placeholder that raises a ProviderError.
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

_RETRYABLE_STATUS = {429, 502, 503, 504}


class GeminiAdapter(ProviderAdapter):
    provider_name = "gemini"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_seconds: float = 30.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds

    def translate_request(self, request: UnifiedChatRequest, *, provider_model: str) -> dict:
        contents: list[dict] = []
        for message in request.messages:
            contents.append(
                {
                    "role": "user" if message.role == "user" else "model",
                    "parts": [{"text": message.content}],
                }
            )

        payload: dict = {
            "model": provider_model,
            "contents": contents,
            "generationConfig": {
                "maxOutputTokens": request.max_tokens,
            },
        }
        if request.temperature is not None:
            payload["generationConfig"]["temperature"] = request.temperature
        if request.top_p is not None:
            payload["generationConfig"]["topP"] = request.top_p
        if request.stop:
            payload["generationConfig"]["stopSequences"] = request.stop
        if request.system:
            payload["systemInstruction"] = {"parts": [{"text": request.system}]}
        return payload

    async def call(self, payload: dict) -> dict:
        # `model` is smuggled into the payload dict only so this method can
        # read it back out to build the URL — Gemini is the one adapter of
        # the four that addresses the model via the request *path*
        # (/models/{model}:generateContent) rather than a body field, unlike
        # OpenAI/Anthropic/Ollama. Send everything else, but strip `model`
        # itself before POSTing so the wire payload matches Gemini's actual
        # documented request schema instead of carrying a stray extra key.
        model = payload.get("model", "")
        body = {k: v for k, v in payload.items() if k != "model"}
        url = f"{self._base_url}/models/{model}:generateContent"
        headers = {"x-goog-api-key": self._api_key}
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=body, headers=headers)
        except httpx.TimeoutException as exc:
            raise ProviderError("gemini request timed out", retryable=True, error_type="timeout") from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"gemini transport error: {exc}", retryable=True, error_type="transport_error"
            ) from exc

        if resp.status_code >= 400:
            _raise_for_status(resp)
        return resp.json()

    def translate_response(
        self, raw: dict, *, request: UnifiedChatRequest, provider_model: str
    ) -> UnifiedChatResponse:
        candidate = raw.get("candidates", [{}])[0]
        content = candidate.get("content", {}).get("parts", [{}])[0].get("text", "")
        usage_raw = raw.get("usageMetadata") or {}
        usage = Usage(
            input_tokens=usage_raw.get("promptTokenCount", 0),
            output_tokens=usage_raw.get("candidatesTokenCount", 0),
        )
        return UnifiedChatResponse(
            id=raw.get("responseId", UnifiedChatResponse.new_id()),
            created=int(time.time()),
            provider=self.provider_name,
            model_requested=request.model,
            model_served=raw.get("model", provider_model),
            choices=[
                UnifiedChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=content),
                    finish_reason=_map_finish_reason(candidate.get("finishReason")),
                )
            ],
            usage=usage,
        )

    async def stream(
        self, payload: dict, *, request: UnifiedChatRequest, provider_model: str
    ) -> AsyncIterator[UnifiedStreamChunk]:
        # NOTE: this function must contain a `yield` even though it always
        # raises. Whether a function is an async *generator* (supports
        # `__anext__`/`async for`) is a static property of its body in
        # Python, not of its return-type annotation or control flow. A
        # bare `async def ...: raise ...` with no `yield` anywhere is just
        # a coroutine function — calling it returns a coroutine object, not
        # an async iterator, and `async for chunk in adapter.stream(...)`
        # in app/api/v1_chat.py fails with a confusing `TypeError` (and a
        # second `AttributeError` from the `agen.aclose()` cleanup) instead
        # of the clean 501 ProviderError this is meant to produce. Worse,
        # because StreamingResponse commits its headers before the body
        # generator runs, that failure surfaces to the client as a silent
        # `200 OK` with an empty body — not an error at all. The unreachable
        # `yield` below is what makes this a real async generator so the
        # ProviderError actually propagates and gets turned into a 502.
        raise ProviderError(
            "gemini streaming is not supported in this phase",
            status_code=501,
            retryable=False,
            error_type="unsupported_streaming",
        )
        yield  # pragma: no cover — unreachable; see note above


def _map_finish_reason(reason: str | None) -> str | None:
    return {"STOP": "stop", "MAX_TOKENS": "length"}.get(reason or "", reason)


def _raise_for_status(resp: httpx.Response) -> None:
    _raise_for_status_bytes(resp.status_code, resp.content)


def _raise_for_status_bytes(status_code: int, body: bytes) -> None:
    try:
        detail = json.loads(body).get("error", {}).get("message", body.decode(errors="replace"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        detail = body.decode(errors="replace")
    error_type = "auth_error" if status_code in (401, 403) else "gemini_error"
    if status_code == 429:
        error_type = "rate_limit_exceeded"
    raise ProviderError(
        f"gemini returned {status_code}: {detail}",
        status_code=status_code,
        retryable=status_code in _RETRYABLE_STATUS,
        error_type=error_type,
    )
