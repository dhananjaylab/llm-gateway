"""
Ollama adapter (native /api/chat, not the /v1 OpenAI-compatible layer).

Built last per the TRD's implementation guide ordering, specifically
because it "forces you to handle the OpenAI-compatible-but-locally-hosted
case": Ollama offers an OpenAI-compatible /v1/chat/completions shim, but
its native /api/chat endpoint is what most of the field-name differences
in the TRD's normalization table refer to (max_tokens -> options.num_predict,
stop -> options.stop), and it streams newline-delimited JSON (NDJSON)
rather than SSE "data: " frames — a third distinct wire format alongside
OpenAI's and Anthropic's. Using the native endpoint here means the adapter
layer has now handled all three shapes a real gateway has to normalize.

Endpoint: POST {base_url}/api/chat
Auth: none for a local install; Authorization: Bearer <OLLAMA_API_KEY> for
Ollama Cloud (base_url=https://ollama.com).
Streaming: NDJSON — one JSON object per line, terminated by a line with
"done": true carrying prompt_eval_count / eval_count as final usage.
"""

from __future__ import annotations

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

_RETRYABLE_STATUS = {429, 502, 503, 504}


class OllamaAdapter(ProviderAdapter):
    provider_name = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
    ) -> None:
        # Local Ollama has no meaningful request-latency SLA comparable to a
        # hosted API, so the default timeout is longer than the hosted
        # adapters' — first-token latency on a cold local model can be slow.
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    # -- request translation -------------------------------------------------

    def translate_request(self, request: UnifiedChatRequest, *, provider_model: str) -> dict:
        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.extend({"role": m.role, "content": m.content} for m in request.messages)

        options: dict = {"num_predict": request.max_tokens}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.top_p is not None:
            options["top_p"] = request.top_p
        if request.stop is not None and request.stop:
            options["stop"] = request.stop

        return {
            "model": provider_model,
            "messages": messages,
            "stream": request.stream,
            "options": options,
        }

    # -- non-streaming call ---------------------------------------------------

    async def call(self, payload: dict) -> dict:
        url = f"{self._base_url}/api/chat"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "ollama request timed out", retryable=True, error_type="timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"ollama transport error: {exc}", retryable=True, error_type="transport_error"
            ) from exc

        if resp.status_code >= 400:
            _raise_for_status(resp)
        return resp.json()

    # -- response translation -------------------------------------------------

    def translate_response(
        self, raw: dict, *, request: UnifiedChatRequest, provider_model: str
    ) -> UnifiedChatResponse:
        usage = Usage(
            input_tokens=raw.get("prompt_eval_count", 0),
            output_tokens=raw.get("eval_count", 0),
        )
        return UnifiedChatResponse(
            # Ollama does not return a response id; mint one so downstream
            # consumers (logs, traces) always have something to key on.
            id=UnifiedChatResponse.new_id(),
            created=int(time.time()),
            provider=self.provider_name,
            model_requested=request.model,
            model_served=raw.get("model", provider_model),
            choices=[
                UnifiedChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant", content=raw.get("message", {}).get("content", "")
                    ),
                    finish_reason="stop" if raw.get("done") else None,
                )
            ],
            usage=usage,
        )

    # -- streaming --------------------------------------------------------------

    async def stream(
        self, payload: dict, *, request: UnifiedChatRequest, provider_model: str
    ) -> AsyncIterator[UnifiedStreamChunk]:
        import json as _json

        url = f"{self._base_url}/api/chat"
        chunk_id = f"gw-{uuid.uuid4().hex[:24]}"

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "POST", url, json=payload, headers=self._headers()
                ) as resp:
                    if resp.status_code >= 400:
                        body = await resp.aread()
                        _raise_for_status_bytes(resp.status_code, body)

                    async for line in resp.aiter_lines():
                        if not line.strip():
                            continue
                        event = _json.loads(line)
                        served_model = event.get("model", provider_model)

                        if event.get("done"):
                            yield UnifiedStreamChunk(
                                id=chunk_id,
                                provider=self.provider_name,
                                model_served=served_model,
                                delta="",
                                finish_reason="stop",
                                usage=Usage(
                                    input_tokens=event.get("prompt_eval_count", 0),
                                    output_tokens=event.get("eval_count", 0),
                                ),
                            )
                            break

                        delta_text = event.get("message", {}).get("content", "")
                        if delta_text:
                            yield UnifiedStreamChunk(
                                id=chunk_id,
                                provider=self.provider_name,
                                model_served=served_model,
                                delta=delta_text,
                            )
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "ollama stream timed out", retryable=True, error_type="timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"ollama stream transport error: {exc}",
                retryable=True,
                error_type="transport_error",
            ) from exc


def _raise_for_status(resp: httpx.Response) -> None:
    _raise_for_status_bytes(resp.status_code, resp.content)


def _raise_for_status_bytes(status_code: int, body: bytes) -> None:
    try:
        import json as _json

        detail = _json.loads(body).get("error", body.decode(errors="replace"))
    except (ValueError, UnicodeDecodeError):
        detail = body.decode(errors="replace")
    raise ProviderError(
        f"ollama returned {status_code}: {detail}",
        status_code=status_code,
        retryable=status_code in _RETRYABLE_STATUS,
        error_type="ollama_error",
    )
