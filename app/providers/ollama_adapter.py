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

Phase 8 additions (tool calling + structured outputs)
──────────────────────────────────────────────────────
  Ollama's native `/api/chat` is already OpenAI-*tools*-shaped on the
  request side (`tools: [{"type":"function","function":{"name",
  "description","parameters"}}]`, one level more nested than OpenAI's own
  flat Responses-API shape) and its tool-result message IS the unified
  `tool` role verbatim (`{"role":"tool","content":...}`) — the one
  provider needing zero structural translation for tool results. There is
  NO `tool_choice` equivalent at all — a non-None `tool_choice` is
  accepted but logged once and has no wire effect (same "log loudly,
  don't crash on an unsupported knob" posture as this codebase's other
  best-effort translations). Ollama also has no call-id concept, so
  `ToolCall.id` is always gateway-synthesized here (`gwsyn_{index}`,
  same convention as GeminiAdapter) — an optional `tool_name` hint is
  attached to outgoing tool-result messages via a same-request id→name
  map, purely to help the model, since Ollama's wire format needs no id
  correlation at all.

  Whether a given LOCAL model's chat template even supports tool calling
  is unknowable ahead of time from the gateway side — an unsupported
  model just answers in prose with `tool_calls` absent from the
  response, which this adapter already treats correctly as "no tool call
  happened," not an error.

  Structured output uses Ollama's native `format` field — the legacy
  `"json"` string for the unified `json_object` mode, or the requested
  JSON Schema object directly for `json_schema`. Confirmed supported by
  Ollama's own docs; **not independently re-verified against a live
  install this pass** (docs/PHASE8_KICKOFF_SCOPING.md §4) — recheck at
  build/deploy time the same way this codebase already re-verifies
  version-pinned facts elsewhere.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import AsyncIterator

import httpx

from app.core.schema import (
    ChatMessage,
    ToolCall,
    UnifiedChatRequest,
    UnifiedChatResponse,
    UnifiedChoice,
    UnifiedStreamChunk,
    Usage,
)
from app.providers.base import ProviderAdapter, ProviderError

logger = logging.getLogger("gateway.ollama_adapter")

_RETRYABLE_STATUS = {429, 502, 503, 504}
_SYNTHETIC_ID_PREFIX = "gwsyn_"


class OllamaAdapter(ProviderAdapter):
    provider_name = "ollama"

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        api_key: str | None = None,
        timeout_seconds: float = 60.0,
        max_connections: int = 50,
        max_keepalive_connections: int = 10,
    ) -> None:
        # Local Ollama has no meaningful request-latency SLA comparable to a
        # hosted API, so the default timeout is longer than the hosted
        # adapters' — first-token latency on a cold local model can be slow.
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        # Phase 7 fix: ONE pooled client held for this adapter's lifetime —
        # see docs/PHASE7_IMPLEMENTATION_GUIDE.md. Smaller default pool
        # than the hosted adapters' — Ollama is typically a single local
        # instance, not a horizontally scaled API.
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            limits=httpx.Limits(
                max_connections=max_connections, max_keepalive_connections=max_keepalive_connections
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}

    # -- request translation -------------------------------------------------

    def translate_request(self, request: UnifiedChatRequest, *, provider_model: str) -> dict:
        messages: list[dict] = []
        if request.system:
            messages.append({"role": "system", "content": request.system})

        call_id_to_name: dict[str, str] = {}
        for m in request.messages:
            if m.role == "assistant" and m.tool_calls:
                tool_calls_payload = []
                for call in m.tool_calls:
                    call_id_to_name[call.id] = call.name
                    try:
                        args = json.loads(call.arguments) if call.arguments else {}
                    except json.JSONDecodeError:
                        args = {}
                    tool_calls_payload.append({"function": {"name": call.name, "arguments": args}})
                messages.append(
                    {"role": "assistant", "content": m.content, "tool_calls": tool_calls_payload}
                )
            elif m.role == "tool":
                tool_message: dict = {"role": "tool", "content": m.content}
                name = call_id_to_name.get(m.tool_call_id)
                if name:
                    tool_message["tool_name"] = name
                messages.append(tool_message)
            else:
                messages.append({"role": m.role, "content": m.content})

        options: dict = {"num_predict": request.max_tokens}
        if request.temperature is not None:
            options["temperature"] = request.temperature
        if request.top_p is not None:
            options["top_p"] = request.top_p
        if request.stop is not None and request.stop:
            options["stop"] = request.stop

        payload: dict = {
            "model": provider_model,
            "messages": messages,
            "stream": request.stream,
            "options": options,
        }

        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
                }
                for t in request.tools
            ]
            if request.tool_choice is not None:
                logger.info(
                    "ollama has no tool_choice equivalent — request.tool_choice=%r is accepted "
                    "but has no wire effect",
                    request.tool_choice,
                )

        if request.response_format is not None:
            if request.response_format.type == "json_schema":
                js = request.response_format.json_schema or {}
                payload["format"] = js["schema"]
            elif request.response_format.type == "json_object":
                payload["format"] = "json"

        return payload

    # -- non-streaming call ---------------------------------------------------

    async def call(self, payload: dict) -> dict:
        url = f"{self._base_url}/api/chat"
        try:
            resp = await self._client.post(url, json=payload, headers=self._headers())
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
        message = raw.get("message") or {}
        tool_calls = _extract_tool_calls(message.get("tool_calls") or [])
        finish_reason = "tool_calls" if tool_calls else ("stop" if raw.get("done") else None)
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
                        role="assistant", content=message.get("content", ""), tool_calls=tool_calls or None
                    ),
                    finish_reason=finish_reason,
                )
            ],
            usage=usage,
        )

    # -- streaming --------------------------------------------------------------

    async def stream(
        self, payload: dict, *, request: UnifiedChatRequest, provider_model: str
    ) -> AsyncIterator[UnifiedStreamChunk]:
        url = f"{self._base_url}/api/chat"
        chunk_id = f"gw-{uuid.uuid4().hex[:24]}"

        try:
            async with self._client.stream(
                "POST", url, json=payload, headers=self._headers()
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    _raise_for_status_bytes(resp.status_code, body)

                async for line in resp.aiter_lines():
                    if not line.strip():
                        continue
                    event = json.loads(line)
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


def _extract_tool_calls(raw_tool_calls: list[dict]) -> list[ToolCall]:
    """Ollama has no call-id concept at all — every ToolCall.id here is
    gateway-synthesized (`gwsyn_{index}`), same convention as
    GeminiAdapter's. `arguments` is usually already a parsed object;
    accept a JSON-string variant too (some Ollama versions/models return
    it that way) rather than assuming one shape."""
    calls: list[ToolCall] = []
    for index, item in enumerate(raw_tool_calls):
        function = item.get("function") or {}
        args = function.get("arguments", {})
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                args = {}
        calls.append(
            ToolCall(
                id=f"{_SYNTHETIC_ID_PREFIX}{index}",
                name=function.get("name", ""),
                arguments=json.dumps(args),
            )
        )
    return calls


def _raise_for_status(resp: httpx.Response) -> None:
    _raise_for_status_bytes(resp.status_code, resp.content)


def _raise_for_status_bytes(status_code: int, body: bytes) -> None:
    try:
        detail = json.loads(body).get("error", body.decode(errors="replace"))
    except (ValueError, UnicodeDecodeError):
        detail = body.decode(errors="replace")
    raise ProviderError(
        f"ollama returned {status_code}: {detail}",
        status_code=status_code,
        retryable=status_code in _RETRYABLE_STATUS,
        error_type="ollama_error",
    )
