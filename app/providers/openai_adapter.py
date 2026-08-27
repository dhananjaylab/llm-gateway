"""
OpenAI adapter (Responses API).

Upstream endpoint: POST {base_url}/v1/responses
Auth:              Authorization: Bearer <OPENAI_API_KEY>

Request shape sent upstream
────────────────────────────
  model              provider model, e.g. "gpt-5.6-sol"
  input              list of {"role": …, "content": …} items (user/assistant turns)
  instructions       system prompt string, omitted when request.system is None
  max_output_tokens  from gateway request.max_tokens
  text.format        {"type": "text"} or {"type": "json_object"}, from request.response_format
  store              always False — preserves the gateway's stateless behavior and avoids
                     OpenAI persisting responses by default
  stream             True/False from request.stream

Response parsing
────────────────
  Non-streaming: prefer raw["output_text"] (string shorthand); otherwise concatenate
  output[].content[] entries where type == "output_text".  Usage lives at
  usage.{input_tokens,output_tokens,input_tokens_details.cached_tokens}.
  Timestamp comes from created_at (int epoch seconds).

Streaming SSE event model
──────────────────────────
  The Responses API uses named SSE events (event: <name>\\ndata: <json>\\n\\n).
  Relevant events:
    response.output_text.delta  → gateway stream delta (data["delta"])
    response.completed          → terminal usage chunk (data["response"]["usage"])
    error                       → raise ProviderError
  Unknown events are silently skipped; the stream ends when the connection closes
  (there is no [DONE] sentinel in the Responses SSE protocol).

GPT-5.x model constraints (verified against OpenAI docs, Phase 4 roster)
──────────────────────────────────────────────────────────────────────────
  stop, temperature, and top_p are all rejected by GPT-5.x models with a 400
  "Unsupported parameter" error — they are never forwarded. If an older model
  (e.g. gpt-4o) re-enters scope, forwarding those fields would need to become
  conditional on provider_model.

HTTP status classification
──────────────────────────
  Retryable:     429, 502, 503, 504
  Non-retryable: 400, 401, 403 (and any other 4xx/5xx)

Phase 8 additions (tool calling + structured outputs)
──────────────────────────────────────────────────────
  tools / tool_choice forward near-verbatim (OpenAI's own `tools`/`tool_choice`
  shape is already the gateway's canonical shape for this adapter). A tool
  call + its result round-trip as two TYPED ITEMS in `input`
  (`function_call` / `function_call_output`), not message roles — see
  `translate_request`'s own docstring. `response_format.type == "json_schema"`
  maps to `text.format = {"type":"json_schema", "name", "schema", "strict"}`.
"""

from __future__ import annotations

import json
import time
import uuid
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

# HTTP statuses the TRD classifies as transient/retryable vs. permanent.
_RETRYABLE_STATUS = {429, 502, 503, 504}


class OpenAIAdapter(ProviderAdapter):
    provider_name = "openai"

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.openai.com",
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

    # -- request translation -------------------------------------------------

    def translate_request(self, request: UnifiedChatRequest, *, provider_model: str) -> dict:
        """Translate the unified gateway request to a Responses API payload.

        NOTE: GPT-5.x models (every OpenAI model currently configured in
        config/tiers.yaml / config/teams.yaml as of the Phase 4 roster
        upgrade to gpt-5.6-sol/-terra) reject stop, temperature, and top_p
        outright with a 400 "Unsupported parameter" error.  None of them are
        forwarded here.  If an older model (e.g. gpt-4o) is added to this
        adapter's scope later, forwarding those fields would need to become
        conditional on provider_model.

        Phase 8: a tool call and its result are two separate TYPED ITEMS in
        `input`, not message roles — this is the OpenAI-specific quirk
        docs/PHASE8_KICKOFF_SCOPING.md §3.1 calls out. A unified assistant
        message carrying `tool_calls` becomes one optional plain-text item
        (if it also has content) plus one `function_call` item per call; a
        unified `tool`-role message becomes one `function_call_output` item.
        `ToolCall.arguments` is already a JSON string (this adapter's own
        canonical shape — see schema.py's ToolCall docstring), so no
        json.dumps/loads is needed on this path, unlike every other adapter.
        """
        input_items: list[dict] = []
        for m in request.messages:
            if m.role == "assistant" and m.tool_calls:
                if m.content:
                    input_items.append({"role": "assistant", "content": m.content})
                for call in m.tool_calls:
                    input_items.append(
                        {
                            "type": "function_call",
                            "call_id": call.id,
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                    )
            elif m.role == "tool":
                input_items.append(
                    {"type": "function_call_output", "call_id": m.tool_call_id, "output": m.content}
                )
            else:
                input_items.append({"role": m.role, "content": m.content})

        payload: dict = {
            "model": provider_model,
            "input": input_items,
            "max_output_tokens": request.max_tokens,
            "store": False,
            "stream": request.stream,
        }

        if request.system:
            payload["instructions"] = request.system

        if request.tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                    "strict": t.strict,
                }
                for t in request.tools
            ]
            if request.tool_choice is not None:
                payload["tool_choice"] = _translate_tool_choice(request.tool_choice)

        if request.response_format is not None:
            if request.response_format.type == "json_schema":
                js = request.response_format.json_schema or {}
                payload["text"] = {
                    "format": {
                        "type": "json_schema",
                        "name": js.get("name", "response"),
                        "schema": js["schema"],
                        "strict": js.get("strict", True),
                    }
                }
            else:
                payload["text"] = {"format": {"type": request.response_format.type}}

        return payload

    # -- non-streaming call ---------------------------------------------------

    async def call(self, payload: dict) -> dict:
        url = f"{self._base_url}/v1/responses"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            resp = await self._client.post(url, json=payload, headers=headers)
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
        """Normalize a Responses API response body to the unified gateway schema.

        Text extraction precedence:
          1. raw["output_text"] — the string shorthand OpenAI populates when the
             response contains exactly one text output item.
          2. Concatenation of output[].content[] entries where type == "output_text".
        """
        usage_raw = raw.get("usage") or {}
        input_tokens_details = usage_raw.get("input_tokens_details") or {}
        usage = Usage(
            input_tokens=usage_raw.get("input_tokens", 0),
            output_tokens=usage_raw.get("output_tokens", 0),
            cache_read_input_tokens=input_tokens_details.get("cached_tokens", 0),
        )
        message_content = _extract_responses_text(raw)
        tool_calls = _extract_responses_tool_calls(raw)
        finish_reason = "tool_calls" if tool_calls else _extract_responses_finish_reason(raw)
        model_served = raw.get("model", provider_model)
        # Responses API returns created_at (int epoch seconds); fall back to
        # local time if absent.
        created = raw.get("created_at", int(time.time()))
        return UnifiedChatResponse(
            id=raw.get("id", UnifiedChatResponse.new_id()),
            created=created,
            provider=self.provider_name,
            model_requested=request.model,
            model_served=model_served,
            choices=[
                UnifiedChoice(
                    index=0,
                    message=ChatMessage(
                        role="assistant", content=message_content, tool_calls=tool_calls or None
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
        """Stream a Responses API SSE response, yielding normalized chunks.

        The Responses API uses named SSE events:
          event: response.output_text.delta   → content delta
          event: response.completed           → terminal usage
          event: error                        → ProviderError

        Each SSE block is a sequence of "field: value" lines followed by a
        blank line.  We accumulate the event name and data across lines and
        dispatch when the block ends.
        """
        url = f"{self._base_url}/v1/responses"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        chunk_id = f"gw-{uuid.uuid4().hex[:24]}"

        try:
            async with self._client.stream("POST", url, json=payload, headers=headers) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    _raise_for_status_bytes(resp.status_code, body)

                event_name: str | None = None
                data_lines: list[str] = []

                async for line in resp.aiter_lines():
                    if line.startswith("event:"):
                        event_name = line[len("event:"):].strip()
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:"):].strip())
                    elif line == "":
                        # Blank line: end of SSE block — dispatch if we have data.
                        if event_name is not None and data_lines:
                            raw_data = "".join(data_lines)
                            try:
                                event_data = json.loads(raw_data)
                            except json.JSONDecodeError:
                                event_name = None
                                data_lines = []
                                continue

                            if event_name == "response.output_text.delta":
                                yield UnifiedStreamChunk(
                                    id=chunk_id,
                                    provider=self.provider_name,
                                    model_served=provider_model,
                                    delta=event_data.get("delta", ""),
                                )

                            elif event_name == "response.completed":
                                response_obj = event_data.get("response") or {}
                                usage_raw = response_obj.get("usage") or {}
                                input_tokens_details = (
                                    usage_raw.get("input_tokens_details") or {}
                                )
                                served_model = response_obj.get("model", provider_model)
                                yield UnifiedStreamChunk(
                                    id=chunk_id,
                                    provider=self.provider_name,
                                    model_served=served_model,
                                    delta="",
                                    finish_reason="stop",
                                    usage=Usage(
                                        input_tokens=usage_raw.get("input_tokens", 0),
                                        output_tokens=usage_raw.get("output_tokens", 0),
                                        cache_read_input_tokens=input_tokens_details.get(
                                            "cached_tokens", 0
                                        ),
                                    ),
                                )

                            elif event_name == "error":
                                message = event_data.get("message", str(event_data))
                                code = event_data.get("code")
                                status_code = int(code) if code and str(code).isdigit() else None
                                retryable = status_code in _RETRYABLE_STATUS if status_code else False
                                raise ProviderError(
                                    f"openai stream error: {message}",
                                    status_code=status_code,
                                    retryable=retryable,
                                    error_type="openai_error",
                                )

                            # Unknown events (response.created, rate_limits, etc.) are skipped.

                        event_name = None
                        data_lines = []

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


def _extract_responses_text(raw: dict) -> str:
    """Extract the assistant text from a Responses API response body.

    Prefers the output_text shorthand string; falls back to concatenating
    typed output[].content[] entries where type == "output_text".
    """
    if isinstance(raw.get("output_text"), str):
        return raw["output_text"]

    parts: list[str] = []
    for item in raw.get("output") or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content") or []:
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                parts.append(content["text"])
    return "".join(parts)


def _extract_responses_finish_reason(raw: dict) -> str | None:
    """Map the Responses API output item status to a gateway finish reason."""
    for item in raw.get("output") or []:
        if item.get("type") == "message":
            status = item.get("status")
            if status == "completed":
                return "stop"
            return status
    return None


def _extract_responses_tool_calls(raw: dict) -> list[ToolCall]:
    """
    Phase 8: `output[]` items with `type == "function_call"` are OpenAI's
    tool-call shape — `arguments` is already the JSON-encoded string this
    gateway's own `ToolCall.arguments` canonically stores, so this is a
    direct passthrough, unlike Anthropic/Gemini/Ollama's parsed-object
    shapes (see each of those adapters' own extraction helpers).
    """
    calls: list[ToolCall] = []
    for item in raw.get("output") or []:
        if item.get("type") == "function_call":
            calls.append(
                ToolCall(
                    id=item.get("call_id") or item.get("id", ""),
                    name=item.get("name", ""),
                    arguments=item.get("arguments", "{}"),
                )
            )
    return calls


def _translate_tool_choice(tool_choice) -> str | dict:
    if isinstance(tool_choice, ForcedToolChoice):
        return {"type": "function", "name": tool_choice.name}
    return tool_choice


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
