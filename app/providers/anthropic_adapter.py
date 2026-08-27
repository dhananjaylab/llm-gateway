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

Phase 8 additions (tool calling + structured outputs)
──────────────────────────────────────────────────────
  Two real structural differences from OpenAI/Ollama, both confirmed
  against current docs (docs/PHASE8_KICKOFF_SCOPING.md §3.2):

  1. Anthropic has NO tool role. A unified `tool`-role message becomes a
     `tool_result` content block inside a **user**-role message.
     Consecutive unified tool messages (parallel calls answered together)
     are coalesced into ONE user message with multiple tool_result blocks
     — see `_build_anthropic_messages` — sending them as separate user
     messages is a malformed conversation from Anthropic's point of view.
  2. A tool call's arguments are a parsed JSON *object* on the wire
     (`tool_use.input`), not the JSON *string* `ToolCall.arguments`
     canonically stores — `json.loads`/`json.dumps` happen at this
     adapter's translation boundary in both directions.

  `tool_choice` is an object, not a bare string: {"type": "auto"|"any"|
  "none"|"tool", "name": ... (tool only)}. Unified "required" maps to
  Anthropic's "any" — NOT a literal "required" string.

  Structured outputs use `output_config.format = {"type":"json_schema",
  "schema":...}` — real, but rolled out per-model-family (Sonnet 4.5/Opus
  4.1+ as of current docs), not universal. Per explicit Phase 8 sign-off
  ("use it directly, but also keep a defensive fallback for unsupported
  models"): `call()` attempts `output_config` first; on a 400 whose body
  signals `output_config` specifically isn't supported for the pinned
  model, it transparently retries ONCE as a single forced tool call
  (`_STRUCTURED_OUTPUT_FALLBACK_TOOL`) — a technique that works on any
  tool-calling-capable Claude model regardless of output_config support.
  `translate_response` unwraps that sentinel tool's `input` back into
  plain text content, so the client never sees that a fallback happened.
  This fallback is non-streaming only — see `stream()`'s own note.
"""

from __future__ import annotations

import json
import logging
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

logger = logging.getLogger("gateway.anthropic_adapter")

_ANTHROPIC_VERSION = "2023-06-01"
_RETRYABLE_STATUS = {429, 502, 503, 504}
# Sentinel tool name for the structured-output capability fallback. Chosen
# to be vanishingly unlikely to collide with a real client-defined tool
# name; if a client ever DOES define a tool with this exact name, the
# fallback path degrades to treating that tool's own forced call as the
# structured-output answer, which is a documented (not silently wrong)
# corner case, not a crash.
_STRUCTURED_OUTPUT_FALLBACK_TOOL = "__gateway_structured_output__"


class AnthropicAdapter(ProviderAdapter):
    provider_name = "anthropic"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.anthropic.com",
        timeout_seconds: float = 30.0,
        max_connections: int = 100,
        max_keepalive_connections: int = 20,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        # Phase 7 fix: ONE pooled client held for this adapter's lifetime,
        # not a fresh httpx.AsyncClient (and therefore a fresh TCP
        # connection pool) constructed and torn down on every call()/
        # stream() — see docs/PHASE7_IMPLEMENTATION_GUIDE.md for the
        # "socket exhaustion at 5,000+ RPS" bottleneck this closes.
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            limits=httpx.Limits(
                max_connections=max_connections, max_keepalive_connections=max_keepalive_connections
            ),
        )

    async def aclose(self) -> None:
        await self._client.aclose()

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
            "messages": _build_anthropic_messages(request.messages),
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

        if request.tools:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                    "strict": t.strict,
                }
                for t in request.tools
            ]
            if request.tool_choice is not None:
                payload["tool_choice"] = _translate_tool_choice(request.tool_choice)

        if request.response_format is not None and request.response_format.type == "json_schema":
            # "json_object" (loose, schema-less JSON) is deliberately left
            # a no-op here, unchanged from pre-Phase-8 behavior — Anthropic
            # has no generic "valid JSON, any shape" mode to map it onto;
            # only the schema-guaranteed case gets translated.
            js = request.response_format.json_schema or {}
            payload["output_config"] = {"format": {"type": "json_schema", "schema": js["schema"]}}

        return payload

    # -- non-streaming call ---------------------------------------------------

    async def call(self, payload: dict) -> dict:
        url = f"{self._base_url}/v1/messages"
        try:
            resp = await self._client.post(url, json=payload, headers=self._headers())
        except httpx.TimeoutException as exc:
            raise ProviderError(
                "anthropic request timed out", retryable=True, error_type="timeout"
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"anthropic transport error: {exc}", retryable=True, error_type="transport_error"
            ) from exc

        if resp.status_code >= 400:
            if "output_config" in payload and _looks_like_unsupported_output_config(
                resp.status_code, resp.content
            ):
                logger.warning(
                    "anthropic model does not support output_config — falling back to a "
                    "forced-tool-call structured-output emulation for this call "
                    "(model=%s)",
                    payload.get("model"),
                )
                return await self._call_structured_output_fallback(payload)
            _raise_for_status(resp)
        return resp.json()

    async def _call_structured_output_fallback(self, payload: dict) -> dict:
        """See module docstring's "Phase 8 additions" section. Retries
        ONCE with `output_config` replaced by a single forced tool call
        whose schema is the originally-requested one — never recurses."""
        schema = payload["output_config"]["format"]["schema"]
        fallback_payload = {k: v for k, v in payload.items() if k != "output_config"}
        fallback_payload["tools"] = [
            *payload.get("tools", []),
            {
                "name": _STRUCTURED_OUTPUT_FALLBACK_TOOL,
                "description": "Emit the final answer as arguments matching the required schema.",
                "input_schema": schema,
            },
        ]
        fallback_payload["tool_choice"] = {"type": "tool", "name": _STRUCTURED_OUTPUT_FALLBACK_TOOL}

        url = f"{self._base_url}/v1/messages"
        try:
            resp = await self._client.post(url, json=fallback_payload, headers=self._headers())
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
        content_blocks = raw.get("content", [])
        fallback_answer = _extract_structured_output_fallback(content_blocks)

        if fallback_answer is not None:
            # The structured-output capability fallback ran (see call()) —
            # unwrap the sentinel tool's arguments back into plain text
            # content; the client asked for a schema-shaped answer, not a
            # tool call, so no ToolCall is surfaced for it.
            text_blocks = [fallback_answer]
            tool_calls: list[ToolCall] = []
            finish_reason = "stop"
        else:
            text_blocks = [b["text"] for b in content_blocks if b.get("type") == "text"]
            tool_calls = _extract_tool_calls(content_blocks)
            finish_reason = "tool_calls" if tool_calls else _map_stop_reason(raw.get("stop_reason"))

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
                    message=ChatMessage(
                        role="assistant", content="".join(text_blocks), tool_calls=tool_calls or None
                    ),
                    finish_reason=finish_reason,
                )
            ],
            usage=usage,
        )

    # -- streaming --------------------------------------------------------------
    #
    # PHASE 8 SCOPE NOTE: tool calling is non-streaming only this phase
    # (docs/PHASE8_KICKOFF_SCOPING.md §5, enforced up front by
    # UnifiedChatRequest's own `_tools_not_yet_supported_with_streaming`
    # validator — a request never reaches this method with both `tools`
    # and `stream=True` set). This loop below still does not parse
    # `content_block_start`/`_delta`(`partial_json`)/`_stop` events for a
    # `tool_use` block, and the structured-output capability fallback in
    # `call()` has no streaming equivalent — a streaming `output_config`
    # request against an unsupported model surfaces as a normal
    # (non-retryable) 400 ProviderError from the initial response status
    # check below, not a silent content loss, which is the safe failure
    # mode until a future phase adds streaming translation for both.

    async def stream(
        self, payload: dict, *, request: UnifiedChatRequest, provider_model: str
    ) -> AsyncIterator[UnifiedStreamChunk]:
        url = f"{self._base_url}/v1/messages"
        message_id = None
        served_model = provider_model
        input_tokens = 0

        try:
            async with self._client.stream(
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
        "tool_use": "tool_calls",
    }.get(reason or "", reason)


def _build_anthropic_messages(messages: list[ChatMessage]) -> list[dict]:
    """
    Phase 8: translate the unified message list into Anthropic's shape,
    handling the two structural differences the module docstring
    describes — no tool role (tool results become `tool_result` blocks
    inside a `user` message) and object-typed (not string) tool-call
    arguments. Every non-tool-calling message (the entire Phase 1-7
    surface) round-trips exactly as before: `{"role": m.role, "content":
    m.content}`.
    """
    out: list[dict] = []
    i = 0
    n = len(messages)
    while i < n:
        message = messages[i]

        if message.role == "assistant" and message.tool_calls:
            content: list[dict] = []
            if message.content:
                content.append({"type": "text", "text": message.content})
            for call in message.tool_calls:
                try:
                    parsed_input = json.loads(call.arguments) if call.arguments else {}
                except json.JSONDecodeError:
                    parsed_input = {}
                content.append(
                    {"type": "tool_use", "id": call.id, "name": call.name, "input": parsed_input}
                )
            out.append({"role": "assistant", "content": content})
            i += 1
            continue

        if message.role == "tool":
            # Coalesce every consecutive tool-role message (parallel calls
            # answered in one batch) into ONE user message with multiple
            # tool_result blocks.
            content = []
            while i < n and messages[i].role == "tool":
                tool_message = messages[i]
                content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_message.tool_call_id,
                        "content": tool_message.content,
                    }
                )
                i += 1
            out.append({"role": "user", "content": content})
            continue

        out.append({"role": message.role, "content": message.content})
        i += 1

    return out


def _translate_tool_choice(tool_choice) -> dict:
    if isinstance(tool_choice, ForcedToolChoice):
        return {"type": "tool", "name": tool_choice.name}
    # Unified "required" -> Anthropic's "any" (NOT a literal "required"
    # string — confirmed against current Claude Platform docs).
    return {"auto": {"type": "auto"}, "none": {"type": "none"}, "required": {"type": "any"}}[tool_choice]


def _extract_tool_calls(content_blocks: list[dict]) -> list[ToolCall]:
    calls: list[ToolCall] = []
    for block in content_blocks:
        if block.get("type") == "tool_use":
            calls.append(
                ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=json.dumps(block.get("input", {})),
                )
            )
    return calls


def _extract_structured_output_fallback(content_blocks: list[dict]) -> str | None:
    """
    If the structured-output capability fallback (call()) is what produced
    this response, its answer lives in the sentinel tool's `input` — this
    extracts and re-serializes it to the JSON string that becomes the
    unified message's plain-text `content`, so the client sees a normal
    schema-shaped text answer with no visible sign a fallback ran.
    """
    for block in content_blocks:
        if block.get("type") == "tool_use" and block.get("name") == _STRUCTURED_OUTPUT_FALLBACK_TOOL:
            return json.dumps(block.get("input", {}))
    return None


def _looks_like_unsupported_output_config(status_code: int, body: bytes) -> bool:
    """
    Heuristic, deliberately conservative: only treat a 400 as "this model
    doesn't support output_config" (and therefore fallback-eligible) when
    the error message actually names `output_config` — every other 400
    (a genuinely malformed schema, an unrelated bad parameter) still
    surfaces as a normal, non-retryable ProviderError instead of silently
    being swallowed into a fallback attempt that would just fail the same
    way again.
    """
    if status_code != 400:
        return False
    try:
        message = (json.loads(body).get("error") or {}).get("message", "")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    lowered = message.lower()
    return "output_config" in lowered and (
        "not support" in lowered or "unsupported" in lowered or "invalid" in lowered
    )


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
