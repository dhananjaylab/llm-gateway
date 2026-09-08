"""
test_streaming_tool_calls.py

Phase 8b test plan (docs/PHASE8B_KICKOFF_SCOPING.md §7): per-provider
streaming tool-call round trips reconstruct byte-identically to the
equivalent non-streaming ToolCall a client would get from the same
request; parallel tool calls are attributed correctly by index;
interleaved text + tool calls both arrive correctly typed; finish_reason
is "tool_calls" exactly when a tool call streamed, "stop" otherwise;
Gemini's existing streaming exclusion is unaffected by tools being
present; and the fallback/billing pipeline carries tool_call_deltas
through end-to-end exactly like it already does for plain text.

Two layers, matching this codebase's own established convention (see
test_error_classification.py's own docstring for the same two-layer
split): adapter-level tests mock each real provider's exact SSE/NDJSON
wire shape via httpx.MockTransport and exercise the real
OpenAIAdapter/AnthropicAdapter/OllamaAdapter.stream() implementations
directly (no HTTP, no fakeredis); end-to-end tests go through the real
FastAPI pipeline via FakeAdapter's Phase 8b `tool_call_chunks` support
(tests/unit/conftest.py) to prove fallback routing, SSE passthrough, and
partial-stream billing all handle tool_call_deltas correctly together.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.core.schema import (
    ChatMessage,
    ToolCall,
    ToolCallDelta,
    ToolDefinition,
    UnifiedChatRequest,
)
from app.providers.anthropic_adapter import AnthropicAdapter
from app.providers.base import ProviderError
from app.providers.gemini_adapter import GeminiAdapter
from app.providers.ollama_adapter import OllamaAdapter
from app.providers.openai_adapter import OpenAIAdapter
from tests.unit.conftest import DATA_SCIENCE_KEY, FakeAdapter, running_app_client

_SHARED_TOOL = ToolDefinition(
    name="get_weather",
    description="Get the current weather for a city.",
    parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
)


def _request(**overrides) -> UnifiedChatRequest:
    defaults = {
        "model": "placeholder:placeholder",
        "messages": [ChatMessage(role="user", content="weather in Pune?")],
        "tools": [_SHARED_TOOL],
        "stream": True,
    }
    defaults.update(overrides)
    return UnifiedChatRequest(**defaults)


def _mock_client(module_path: str, monkeypatch, handler) -> None:
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_async_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(f"{module_path}.httpx.AsyncClient", factory)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def _collect(agen) -> list:
    return [chunk async for chunk in agen]


def _reconstruct_tool_calls(chunks: list) -> list[ToolCall]:
    """
    The client-side accumulation contract documented on ToolCallDelta
    itself (app/core/schema.py) and in docs/PHASE8B_KICKOFF_SCOPING.md
    §2.1: group deltas by `index` in order of first appearance, keep the
    `id`/`name` from the first delta seen for that index, concatenate
    `arguments_delta` in arrival order. Deliberately provider-agnostic —
    this same function is reused across every provider's test below,
    which is the whole point of normalizing to one wire shape.
    """
    order: list[int] = []
    ids: dict[int, str] = {}
    names: dict[int, str] = {}
    fragments: dict[int, list[str]] = {}

    for chunk in chunks:
        for delta in chunk.tool_call_deltas or []:
            if delta.index not in fragments:
                order.append(delta.index)
                fragments[delta.index] = []
            if delta.id is not None:
                ids[delta.index] = delta.id
            if delta.name is not None:
                names[delta.index] = delta.name
            fragments[delta.index].append(delta.arguments_delta)

    return [
        ToolCall(id=ids[i], name=names[i], arguments="".join(fragments[i]))
        for i in order
    ]


# ============================================================================
# OpenAI
# ============================================================================


async def test_openai_streaming_tool_call_reconstructs_to_the_non_streaming_shape(monkeypatch):
    body = "".join(
        [
            _sse(
                "response.output_item.added",
                {
                    "output_index": 0,
                    "item": {"type": "function_call", "call_id": "call_abc", "name": "get_weather"},
                },
            ),
            _sse("response.function_call_arguments.delta", {"output_index": 0, "delta": '{"city": '}),
            _sse("response.function_call_arguments.delta", {"output_index": 0, "delta": '"Pune"}'}),
            _sse(
                "response.completed",
                {"response": {"model": "gpt-5.6-sol", "usage": {"input_tokens": 5, "output_tokens": 3}}},
            ),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    _mock_client("app.providers.openai_adapter", monkeypatch, handler)
    adapter = OpenAIAdapter(api_key="k")
    req = _request(model="openai:gpt-5.6-sol")
    payload = adapter.translate_request(req, provider_model="gpt-5.6-sol")

    chunks = await _collect(adapter.stream(payload, request=req, provider_model="gpt-5.6-sol"))
    calls = _reconstruct_tool_calls(chunks)

    assert calls == [ToolCall(id="call_abc", name="get_weather", arguments='{"city": "Pune"}')]
    assert chunks[-1].finish_reason == "tool_calls"
    assert chunks[-1].usage.output_tokens == 3
    await adapter.aclose()


async def test_openai_streaming_parallel_tool_calls_are_attributed_by_index(monkeypatch):
    body = "".join(
        [
            _sse(
                "response.output_item.added",
                {"output_index": 0, "item": {"type": "function_call", "call_id": "call_a", "name": "f1"}},
            ),
            _sse(
                "response.output_item.added",
                {"output_index": 1, "item": {"type": "function_call", "call_id": "call_b", "name": "f2"}},
            ),
            _sse("response.function_call_arguments.delta", {"output_index": 0, "delta": '{"x":'}),
            _sse("response.function_call_arguments.delta", {"output_index": 1, "delta": '{"y":'}),
            _sse("response.function_call_arguments.delta", {"output_index": 0, "delta": "1}"}),
            _sse("response.function_call_arguments.delta", {"output_index": 1, "delta": "2}"}),
            _sse("response.completed", {"response": {"model": "gpt-5.6-sol", "usage": {}}}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    _mock_client("app.providers.openai_adapter", monkeypatch, handler)
    adapter = OpenAIAdapter(api_key="k")
    req = _request(model="openai:gpt-5.6-sol")
    payload = adapter.translate_request(req, provider_model="gpt-5.6-sol")

    chunks = await _collect(adapter.stream(payload, request=req, provider_model="gpt-5.6-sol"))
    calls = _reconstruct_tool_calls(chunks)

    assert calls == [
        ToolCall(id="call_a", name="f1", arguments='{"x":1}'),
        ToolCall(id="call_b", name="f2", arguments='{"y":2}'),
    ]
    await adapter.aclose()


async def test_openai_streaming_text_only_still_reports_finish_reason_stop(monkeypatch):
    """Regression guard: a response with no function_call items must not
    spuriously report finish_reason="tool_calls" just because tools were
    offered on the request."""
    body = "".join(
        [
            _sse("response.output_text.delta", {"delta": "hi"}),
            _sse("response.completed", {"response": {"model": "gpt-5.6-sol", "usage": {}}}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    _mock_client("app.providers.openai_adapter", monkeypatch, handler)
    adapter = OpenAIAdapter(api_key="k")
    req = _request(model="openai:gpt-5.6-sol")
    payload = adapter.translate_request(req, provider_model="gpt-5.6-sol")

    chunks = await _collect(adapter.stream(payload, request=req, provider_model="gpt-5.6-sol"))
    assert chunks[-1].finish_reason == "stop"
    assert all(c.tool_call_deltas is None for c in chunks)
    await adapter.aclose()


# ============================================================================
# Anthropic
# ============================================================================


async def test_anthropic_streaming_tool_call_reconstructs_to_the_non_streaming_shape(monkeypatch):
    body = "".join(
        [
            _sse(
                "message_start",
                {"message": {"id": "msg_1", "model": "claude-sonnet-5", "usage": {"input_tokens": 5}}},
            ),
            _sse(
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {
                        "type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}
                    },
                },
            ),
            _sse(
                "content_block_delta",
                {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"city": '}},
            ),
            _sse(
                "content_block_delta",
                {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '"Pune"}'}},
            ),
            _sse("content_block_stop", {"index": 0}),
            _sse("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 3}}),
            _sse("message_stop", {}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    _mock_client("app.providers.anthropic_adapter", monkeypatch, handler)
    adapter = AnthropicAdapter(api_key="k")
    req = _request(model="anthropic:claude-sonnet-5")
    payload = adapter.translate_request(req, provider_model="claude-sonnet-5")

    chunks = await _collect(adapter.stream(payload, request=req, provider_model="claude-sonnet-5"))
    calls = _reconstruct_tool_calls(chunks)

    assert calls == [ToolCall(id="toolu_1", name="get_weather", arguments='{"city": "Pune"}')]
    assert chunks[-1].finish_reason == "tool_calls"
    assert chunks[-1].usage.output_tokens == 3
    await adapter.aclose()


async def test_anthropic_streaming_parallel_tool_calls_are_attributed_by_index(monkeypatch):
    body = "".join(
        [
            _sse(
                "message_start",
                {"message": {"id": "msg_2", "model": "claude-sonnet-5", "usage": {"input_tokens": 5}}},
            ),
            _sse(
                "content_block_start",
                {
                    "index": 0,
                    "content_block": {"type": "tool_use", "id": "toolu_a", "name": "f1", "input": {}},
                },
            ),
            _sse(
                "content_block_start",
                {
                    "index": 1,
                    "content_block": {"type": "tool_use", "id": "toolu_b", "name": "f2", "input": {}},
                },
            ),
            _sse(
                "content_block_delta",
                {"index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"x":1}'}},
            ),
            _sse(
                "content_block_delta",
                {"index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"y":2}'}},
            ),
            _sse("content_block_stop", {"index": 0}),
            _sse("content_block_stop", {"index": 1}),
            _sse("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 6}}),
            _sse("message_stop", {}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    _mock_client("app.providers.anthropic_adapter", monkeypatch, handler)
    adapter = AnthropicAdapter(api_key="k")
    req = _request(model="anthropic:claude-sonnet-5")
    payload = adapter.translate_request(req, provider_model="claude-sonnet-5")

    chunks = await _collect(adapter.stream(payload, request=req, provider_model="claude-sonnet-5"))
    calls = _reconstruct_tool_calls(chunks)

    assert calls == [
        ToolCall(id="toolu_a", name="f1", arguments='{"x":1}'),
        ToolCall(id="toolu_b", name="f2", arguments='{"y":2}'),
    ]
    await adapter.aclose()


async def test_anthropic_streaming_interleaves_text_and_tool_call_blocks(monkeypatch):
    """A response streaming some text (index 0) then a tool call (index 1)
    — both must arrive correctly typed in the same loop."""
    body = "".join(
        [
            _sse(
                "message_start",
                {"message": {"id": "msg_3", "model": "claude-sonnet-5", "usage": {"input_tokens": 5}}},
            ),
            _sse(
                "content_block_delta",
                {"index": 0, "delta": {"type": "text_delta", "text": "Let me check. "}},
            ),
            _sse(
                "content_block_start",
                {
                    "index": 1,
                    "content_block": {
                        "type": "tool_use", "id": "toolu_c", "name": "get_weather", "input": {}
                    },
                },
            ),
            _sse(
                "content_block_delta",
                {"index": 1, "delta": {"type": "input_json_delta", "partial_json": '{"city":"Pune"}'}},
            ),
            _sse("content_block_stop", {"index": 1}),
            _sse("message_delta", {"delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 4}}),
            _sse("message_stop", {}),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    _mock_client("app.providers.anthropic_adapter", monkeypatch, handler)
    adapter = AnthropicAdapter(api_key="k")
    req = _request(model="anthropic:claude-sonnet-5")
    payload = adapter.translate_request(req, provider_model="claude-sonnet-5")

    chunks = await _collect(adapter.stream(payload, request=req, provider_model="claude-sonnet-5"))

    text_chunks = [c for c in chunks if c.delta]
    assert text_chunks[0].delta == "Let me check. "
    calls = _reconstruct_tool_calls(chunks)
    assert calls == [ToolCall(id="toolu_c", name="get_weather", arguments='{"city":"Pune"}')]
    assert chunks[-1].finish_reason == "tool_calls"
    await adapter.aclose()


# ============================================================================
# Ollama
# ============================================================================


async def test_ollama_streaming_tool_call_reconstructs_to_the_non_streaming_shape(monkeypatch):
    lines = [
        json.dumps(
            {
                "model": "llama3.2",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Pune"}}}],
                },
                "done": False,
            }
        ),
        json.dumps(
            {"model": "llama3.2", "message": {"role": "assistant", "content": ""}, "done": True,
             "prompt_eval_count": 5, "eval_count": 3}
        ),
    ]
    body = "\n".join(lines) + "\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "application/x-ndjson"})

    _mock_client("app.providers.ollama_adapter", monkeypatch, handler)
    adapter = OllamaAdapter(base_url="http://localhost:11434")
    req = _request(model="ollama:llama3.2")
    payload = adapter.translate_request(req, provider_model="llama3.2")

    chunks = await _collect(adapter.stream(payload, request=req, provider_model="llama3.2"))
    calls = _reconstruct_tool_calls(chunks)

    # Ollama synthesizes ids (gwsyn_{index}) — same convention the
    # non-streaming path already uses via the same _extract_tool_calls().
    assert len(calls) == 1
    assert calls[0].id.startswith("gwsyn_")
    assert calls[0].name == "get_weather"
    assert json.loads(calls[0].arguments) == {"city": "Pune"}
    assert chunks[-1].finish_reason == "tool_calls"
    assert chunks[-1].usage.output_tokens == 3
    await adapter.aclose()


async def test_ollama_streaming_delivers_the_whole_argument_string_in_one_delta(monkeypatch):
    """Confirms the documented asymmetry (docs/PHASE8B_KICKOFF_SCOPING.md
    §0 finding 3): unlike OpenAI/Anthropic, Ollama's tool call arrives as
    exactly ONE delta per index, not several fragments."""
    lines = [
        json.dumps(
            {
                "model": "llama3.2",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Pune"}}}],
                },
                "done": False,
            }
        ),
        json.dumps({"model": "llama3.2", "message": {"content": ""}, "done": True}),
    ]
    body = "\n".join(lines) + "\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "application/x-ndjson"})

    _mock_client("app.providers.ollama_adapter", monkeypatch, handler)
    adapter = OllamaAdapter(base_url="http://localhost:11434")
    req = _request(model="ollama:llama3.2")
    payload = adapter.translate_request(req, provider_model="llama3.2")

    chunks = await _collect(adapter.stream(payload, request=req, provider_model="llama3.2"))
    tool_chunks = [c for c in chunks if c.tool_call_deltas]
    assert len(tool_chunks) == 1
    assert len(tool_chunks[0].tool_call_deltas) == 1
    await adapter.aclose()


async def test_ollama_streaming_saw_tool_calls_flag_holds_even_if_done_chunk_carries_none(monkeypatch):
    """finish_reason must reflect that a tool call happened EARLIER in the
    stream, even though the terminating done:true line carries no
    tool_calls of its own — see the adapter's own docstring on why this
    is tracked across the whole stream, not read off only the last line."""
    lines = [
        json.dumps(
            {
                "model": "llama3.2",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [{"function": {"name": "f", "arguments": {}}}],
                },
                "done": False,
            }
        ),
        json.dumps({"model": "llama3.2", "message": {"content": ""}, "done": True, "eval_count": 1}),
    ]
    body = "\n".join(lines) + "\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body.encode(), headers={"content-type": "application/x-ndjson"})

    _mock_client("app.providers.ollama_adapter", monkeypatch, handler)
    adapter = OllamaAdapter(base_url="http://localhost:11434")
    req = _request(model="ollama:llama3.2")
    payload = adapter.translate_request(req, provider_model="llama3.2")

    chunks = await _collect(adapter.stream(payload, request=req, provider_model="llama3.2"))
    assert chunks[-1].finish_reason == "tool_calls"


# ============================================================================
# Gemini — confirmed exclusion (docs/PHASE8B_KICKOFF_SCOPING.md §3.4, §8 Q1)
# ============================================================================


async def test_gemini_streaming_still_raises_unsupported_even_with_tools_present():
    """Phase 8b makes no change to GeminiAdapter — its stream() must keep
    unconditionally rejecting every streaming attempt, tools or not, so a
    tier chain that falls back all the way to Gemini for a streaming+tools
    request still cleanly surfaces the existing 501, not a new failure
    shape this phase silently introduced."""
    adapter = GeminiAdapter(api_key="k")
    req = _request(model="gemini:gemini-3.6-flash")
    payload = adapter.translate_request(req, provider_model="gemini-3.6-flash")

    gen = adapter.stream(payload, request=req, provider_model="gemini-3.6-flash")
    with pytest.raises(ProviderError) as exc_info:
        async for _ in gen:
            pass
    assert exc_info.value.status_code == 501
    assert exc_info.value.error_type == "unsupported_streaming"


# ============================================================================
# End-to-end through the real HTTP pipeline (FakeAdapter, no mocked SSE)
# ============================================================================


def _sse_data_frames(raw_text: str) -> list[dict]:
    frames = []
    for block in raw_text.split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                frames.append(json.loads(line[len("data:") :].strip()))
    return frames


def test_streaming_request_with_tools_is_accepted_by_the_real_endpoint(client, monkeypatch):
    """The core Phase 8b regression: a request that Phase 8's schema guard
    would have 422'd must now reach the provider and stream normally."""
    fake = FakeAdapter(
        stream_chunks=[],
        tool_call_chunks=[
            [ToolCallDelta(index=0, id="call_1", name="get_weather", arguments_delta='{"city":"Pune"}')]
        ],
    )
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "openai:gpt-5.4",
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [_SHARED_TOOL.model_dump()],
            "stream": True,
        },
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    ) as resp:
        assert resp.status_code == 200
        raw_text = resp.read().decode()

    frames = _sse_data_frames(raw_text)
    tool_frames = [f for f in frames if f.get("tool_call_deltas")]
    assert len(tool_frames) == 1
    assert tool_frames[0]["tool_call_deltas"][0]["name"] == "get_weather"
    assert frames[-1]["finish_reason"] == "tool_calls"


async def test_fallback_activates_for_a_streaming_tools_request_before_any_chunk(app, monkeypatch):
    """Extends test_fallback_chain.py's own pre-first-chunk fallback
    pattern with a tools-carrying request — proves
    FallbackRouter.stream_with_fallback needs no changes (per
    docs/PHASE8B_KICKOFF_SCOPING.md §5): it already commits on "first
    chunk received," never on chunk content, so a tool-call-carrying first
    chunk is handled correctly with zero special-casing."""
    failing = FakeAdapter(always_fail=True, retryable=True, error_type="timeout")
    working = FakeAdapter(
        stream_chunks=[],
        tool_call_chunks=[
            [ToolCallDelta(index=0, id="call_1", name="get_weather", arguments_delta='{"city":"Pune"}')]
        ],
    )

    def _resolve(model_id: str):
        if model_id == "openai:gpt-5.6-sol":
            return failing, "gpt-5.6-sol-served"
        if model_id == "anthropic:claude-sonnet-5":
            return working, "claude-sonnet-5-served"
        raise AssertionError(model_id)

    monkeypatch.setattr("app.api.v1_chat.resolve_model", _resolve)

    async with running_app_client(app) as client:
        await app.state.team_store.update_team("data-science", {"allowed_models": ["tier-1-reasoning"]})
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "tier-1-reasoning",
                "messages": [{"role": "user", "content": "weather?"}],
                "tools": [_SHARED_TOOL.model_dump()],
                "stream": True,
            },
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        ) as resp:
            assert resp.status_code == 200
            raw_text = "".join([chunk async for chunk in resp.aiter_text()])

    assert "event: error" not in raw_text
    frames = _sse_data_frames(raw_text)
    assert any(f.get("tool_call_deltas") for f in frames)
    assert failing.call_count == 3  # RETRY_MAX_ATTEMPTS (conftest.py) — retried before falling back
    assert working.call_count == 1


async def test_mid_stream_failure_during_tool_call_bills_the_partial_arguments_sent(
    client, monkeypatch, admin_headers
):
    """
    Phase 8b regression guard for §4: a mid-stream failure while a tool
    call's arguments are still accumulating must still bill the partial
    argument text sent so far, the same way a mid-stream text failure
    already does (see test_fallback_chain.py's
    test_streaming_mid_stream_failure_still_bills_the_partial_content and
    docs/PHASE8B_KICKOFF_SCOPING.md §4/§5). Uses a hand-rolled adapter
    (not FakeAdapter's tool_call_chunks) so the failure happens
    mid-argument-accumulation, after one tool_call_delta chunk already
    reached the client, exactly the "novel failure shape" §5 documents.
    """
    from app.core.pricing import ModelPricing
    from app.core.schema import UnifiedStreamChunk
    from app.providers.base import ProviderError

    class _FailsMidToolCall(FakeAdapter):
        async def stream(self, payload, *, request, provider_model):
            self.call_count += 1
            yield UnifiedStreamChunk(
                id="mid-1",
                provider=self.provider_name,
                model_served=provider_model,
                tool_call_deltas=[
                    ToolCallDelta(index=0, id="call_1", name="get_weather", arguments_delta='{"city":')
                ],
            )
            raise ProviderError("dropped mid-argument", retryable=True, error_type="timeout")

    primary = _FailsMidToolCall()
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (primary, "gpt-5.4-served"))
    client.app.state.pricing["fake:gpt-5.4-served"] = ModelPricing(
        input_per_million=2.0, output_per_million=10.0
    )

    budget_before = client.get("/admin/budgets/data-science", headers=admin_headers).json()["spend_usd"]

    with client.stream(
        "POST",
        "/v1/chat/completions",
        json={
            "model": "openai:gpt-5.4",
            "messages": [{"role": "user", "content": "weather?"}],
            "tools": [_SHARED_TOOL.model_dump()],
            "stream": True,
        },
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    ) as resp:
        assert resp.status_code == 200
        raw_text = resp.read().decode()

    assert "event: error" in raw_text
    budget_after = client.get("/admin/budgets/data-science", headers=admin_headers).json()["spend_usd"]
    assert budget_after > budget_before, (
        "the partial tool-call argument fragment sent before the mid-stream failure "
        f"must be billed — spend was {budget_before} before and {budget_after} after"
    )


async def test_full_chain_exhausted_by_three_fakes_then_gemini_bubbles_its_own_501_not_a_generic_exhaustion(
    app, monkeypatch
):
    """
    Uses config/tiers.yaml's real tier-1-reasoning chain (openai ->
    anthropic -> ollama -> gemini) with the first three links faked to
    fail retryably and the REAL GeminiAdapter standing in for the last
    link. Confirms the actual, easy-to-miss interaction this phase
    introduces: Gemini's streaming rejection is NON-retryable, so it
    aborts the chain walk immediately with its own 501
    "unsupported_streaming" error — it does NOT get reclassified as a
    generic "fallback_chain_exhausted" 503, even though every link in the
    chain ultimately failed. Document 03's "a bad request is a bad
    request on every provider" rule applies here to "streaming isn't
    implemented for this provider" exactly the way it already applies to
    a genuine 400/401/403 — see docs/PHASE8B_KICKOFF_SCOPING.md §5.
    """
    openai_fake = FakeAdapter(always_fail=True, retryable=True, error_type="timeout")
    anthropic_fake = FakeAdapter(always_fail=True, retryable=True, error_type="timeout")
    ollama_fake = FakeAdapter(always_fail=True, retryable=True, error_type="timeout")
    real_gemini = GeminiAdapter(api_key="test-gemini-key")

    def _resolve(model_id: str):
        return {
            "openai:gpt-5.6-sol": (openai_fake, "gpt-5.6-sol-served"),
            "anthropic:claude-sonnet-5": (anthropic_fake, "claude-sonnet-5-served"),
            "ollama:llama3.2": (ollama_fake, "llama3.2-served"),
            "gemini:gemini-3.6-flash": (real_gemini, "gemini-3.6-flash"),
        }[model_id]

    monkeypatch.setattr("app.api.v1_chat.resolve_model", _resolve)

    try:
        async with running_app_client(app) as client:
            await app.state.team_store.update_team("data-science", {"allowed_models": ["tier-1-reasoning"]})
            async with client.stream(
                "POST",
                "/v1/chat/completions",
                json={
                    "model": "tier-1-reasoning",
                    "messages": [{"role": "user", "content": "weather?"}],
                    "tools": [_SHARED_TOOL.model_dump()],
                    "stream": True,
                },
                headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
            ) as resp:
                assert resp.status_code == 200
                raw_text = "".join([chunk async for chunk in resp.aiter_text()])
    finally:
        await real_gemini.aclose()

    assert "event: error" in raw_text
    assert "unsupported_streaming" in raw_text
    assert "fallback_chain_exhausted" not in raw_text
    assert openai_fake.call_count == 3  # RETRY_MAX_ATTEMPTS, exhausted before advancing
    assert anthropic_fake.call_count == 3
    assert ollama_fake.call_count == 3
