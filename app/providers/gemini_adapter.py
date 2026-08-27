"""
Gemini adapter (Google Generative AI API).

Endpoint: POST {base_url}/v1beta2/models/{model}:generateContent
Auth: x-goog-api-key: <GEMINI_API_KEY>
Streaming: SSE-like chunked responses from the Google API are not yet
implemented in this Phase 1 integration; the adapter supports non-streaming
calls and a basic streaming placeholder that raises a ProviderError.

Phase 8 additions (tool calling + structured outputs)
──────────────────────────────────────────────────────
  Two real quirks confirmed against current (Aug 2026) official Gemini API
  docs (docs/PHASE8_KICKOFF_SCOPING.md §3.3), both handled explicitly
  rather than silently:

  1. Schema case convention. Gemini's `functionDeclarations[].parameters`
     and `generationConfig.responseSchema` use an OpenAPI-Schema-Object
     dialect with `type` as an UPPERCASE enum (OBJECT/STRING/NUMBER/...),
     not standard lowercase JSON Schema — `_to_gemini_schema` recursively
     converts the gateway's canonical (OpenAI/Anthropic-flavored,
     lowercase) schema, and raises a clear ProviderError for JSON Schema
     keywords Gemini's subset doesn't support ($ref, oneOf, anyOf, allOf)
     rather than silently dropping them.
  2. Call correlation. Confirmed against Google's current official
     "Function calling with the Gemini API" doc: newer Gemini versions DO
     echo an `id` on `functionCall` for parallel-call disambiguation, but
     it's optional and not always present. `_extract_tool_calls`
     preserves a REAL id when Gemini supplies one; only when it's absent
     does this adapter synthesize one (`f"gwsyn_{index}"`, a
     gateway-owned prefix unlikely to ever collide with a real Gemini id)
     purely so `ToolCall.id` is never empty. `_build_gemini_contents`
     never sends a synthetic id back to Gemini — it looks the call's
     `name` up from a same-request id→name map built while walking
     history instead, matching Gemini's own confirmed shape: the
     `functionResponse` turn is `role: "user"` (NOT `role: "function"` —
     that's a long-standing, still-open feature *request* against the
     real API, confirmed via a still-open GitHub issue, not a shipped
     option).

  A `tool`-role message's plain-text `content` is wrapped as
  `{"result": content}` for the `functionResponse.response` object, since
  Gemini expects a JSON object there and the gateway has no more specific
  key name to offer — a documented, gateway-owned convention, not a
  Gemini requirement.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator

import httpx

from app.core.schema import (
    ChatMessage,
    ForcedToolChoice,
    ToolCall,
    UnifiedChatRequest,
    UnifiedChatResponse,
    UnifiedChoice,
    UnifiedStreamChunk,
    Usage,
)
from app.providers.base import ProviderAdapter, ProviderError

_RETRYABLE_STATUS = {429, 502, 503, 504}

# Gateway-owned prefix for a synthesized (not provider-issued) tool-call
# id — see module docstring point 2. Never sent back to Gemini.
_SYNTHETIC_ID_PREFIX = "gwsyn_"

_JSON_SCHEMA_TYPE_TO_GEMINI = {
    "object": "OBJECT",
    "string": "STRING",
    "number": "NUMBER",
    "integer": "INTEGER",
    "boolean": "BOOLEAN",
    "array": "ARRAY",
    "null": "NULL",
}
_UNSUPPORTED_SCHEMA_KEYWORDS = {"oneOf", "anyOf", "allOf", "$ref", "$defs", "not", "if", "then", "else"}


class GeminiAdapter(ProviderAdapter):
    provider_name = "gemini"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout_seconds: float = 30.0,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        # Phase 7 fix: ONE pooled client held for this adapter's lifetime —
        # see docs/PHASE7_IMPLEMENTATION_GUIDE.md.
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            limits=httpx.Limits(
                max_connections=max_connections, max_keepalive_connections=max_keepalive_connections
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def translate_request(self, request: UnifiedChatRequest, *, provider_model: str) -> dict:
        payload: dict = {
            "model": provider_model,
            "contents": _build_gemini_contents(request.messages),
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

        if request.tools:
            payload["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": _to_gemini_schema(t.parameters),
                        }
                        for t in request.tools
                    ]
                }
            ]
            payload["toolConfig"] = {"functionCallingConfig": _translate_tool_choice(request.tool_choice)}

        if request.response_format is not None and request.response_format.type == "json_schema":
            js = request.response_format.json_schema or {}
            payload["generationConfig"]["responseMimeType"] = "application/json"
            payload["generationConfig"]["responseSchema"] = _to_gemini_schema(js["schema"])

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
            resp = await self._client.post(url, json=body, headers=headers)
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
        parts = candidate.get("content", {}).get("parts") or [{}]
        text = "".join(p.get("text", "") for p in parts if "text" in p)
        tool_calls = _extract_tool_calls(parts)
        finish_reason = "tool_calls" if tool_calls else _map_finish_reason(candidate.get("finishReason"))
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
                    message=ChatMessage(role="assistant", content=text, tool_calls=tool_calls or None),
                    finish_reason=finish_reason,
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


def _build_gemini_contents(messages: list[ChatMessage]) -> list[dict]:
    """
    See module docstring point 2 for the confirmed-current wire shape:
    functionCall lives on a `role: "model"` turn, functionResponse on a
    `role: "user"` turn (NOT `role: "function"` — that's an open feature
    request against the real API, not a shipped option). `call_id_to_name`
    is purely gateway-internal bookkeeping: Gemini's `functionResponse`
    needs the tool's `name`, which the unified `tool`-role message (keyed
    only by `tool_call_id`) doesn't carry directly.
    """
    contents: list[dict] = []
    call_id_to_name: dict[str, str] = {}

    for message in messages:
        if message.role == "assistant" and message.tool_calls:
            parts: list[dict] = []
            if message.content:
                parts.append({"text": message.content})
            for call in message.tool_calls:
                call_id_to_name[call.id] = call.name
                try:
                    args = json.loads(call.arguments) if call.arguments else {}
                except json.JSONDecodeError:
                    args = {}
                function_call: dict = {"name": call.name, "args": args}
                if not call.id.startswith(_SYNTHETIC_ID_PREFIX):
                    function_call["id"] = call.id
                parts.append({"functionCall": function_call})
            contents.append({"role": "model", "parts": parts})
            continue

        if message.role == "tool":
            function_response: dict = {
                "name": call_id_to_name.get(message.tool_call_id, ""),
                "response": {"result": message.content},
            }
            if message.tool_call_id and not message.tool_call_id.startswith(_SYNTHETIC_ID_PREFIX):
                function_response["id"] = message.tool_call_id
            contents.append({"role": "user", "parts": [{"functionResponse": function_response}]})
            continue

        contents.append(
            {"role": "user" if message.role == "user" else "model", "parts": [{"text": message.content}]}
        )

    return contents


def _extract_tool_calls(parts: list[dict]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for index, part in enumerate(parts):
        function_call = part.get("functionCall")
        if function_call:
            call_id = function_call.get("id") or f"{_SYNTHETIC_ID_PREFIX}{index}"
            calls.append(
                ToolCall(
                    id=call_id,
                    name=function_call.get("name", ""),
                    arguments=json.dumps(function_call.get("args", {})),
                )
            )
    return calls


def _translate_tool_choice(tool_choice) -> dict:
    if tool_choice is None:
        return {"mode": "AUTO"}
    if isinstance(tool_choice, ForcedToolChoice):
        return {"mode": "ANY", "allowedFunctionNames": [tool_choice.name]}
    return {"auto": {"mode": "AUTO"}, "none": {"mode": "NONE"}, "required": {"mode": "ANY"}}[tool_choice]


def _to_gemini_schema(schema: dict) -> dict:
    """
    Recursively translate the gateway's canonical (lowercase-`type`) JSON
    Schema into Gemini's uppercase-enum OpenAPI-Schema-Object dialect —
    see module docstring point 1. Raises a clear, non-retryable
    ProviderError for keywords Gemini's subset doesn't support, rather
    than silently dropping them and letting Gemini either 400 on its own
    terms or (worse) silently ignore part of the requested shape.
    """
    unsupported = _UNSUPPORTED_SCHEMA_KEYWORDS & schema.keys()
    if unsupported:
        raise ProviderError(
            f"gemini: JSON Schema keyword(s) {sorted(unsupported)} are not expressible in "
            "Gemini's OpenAPI-subset schema dialect",
            retryable=False,
            error_type="unsupported_schema_feature",
        )
    out: dict = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, str):
            out[key] = _JSON_SCHEMA_TYPE_TO_GEMINI.get(value.lower(), value.upper())
        elif key == "properties" and isinstance(value, dict):
            out[key] = {name: _to_gemini_schema(prop) for name, prop in value.items()}
        elif key == "items" and isinstance(value, dict):
            out[key] = _to_gemini_schema(value)
        else:
            out[key] = value
    return out


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
