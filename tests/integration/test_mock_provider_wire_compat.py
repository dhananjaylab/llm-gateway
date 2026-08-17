"""
test_mock_provider_wire_compat.py

Verifies that deploy/mock-providers/main.py speaks the exact wire format
each real adapter in app/providers/*.py already parses -- by running the
REAL adapter code (translate_request -> call/stream ->
translate_response) against the mock's ASGI app directly, via
`httpx.ASGITransport`. No network, no Docker, no live stack: this is a
pure in-process check, so it runs in the ordinary `pytest` invocation
alongside the unit suite, not gated behind `requires_live_stack`.

This is deliberately the strongest form of "does the mock match the
real API" check available without spending real provider credits: if a
future edit to an adapter's parsing logic (or to the mock's response
shape) breaks compatibility, THIS is the test that catches it -- a test
that only asserted the mock's own JSON shape by hand could drift from
what the adapter actually reads without either side noticing.
"""

from __future__ import annotations

import httpx
import pytest
from main import app as mock_app  # deploy/mock-providers/main.py, via conftest's sys.path insert
from main import chaos as mock_chaos

from app.core.schema import ChatMessage, UnifiedChatRequest
from app.providers.anthropic_adapter import AnthropicAdapter
from app.providers.base import ProviderError
from app.providers.gemini_adapter import GeminiAdapter
from app.providers.ollama_adapter import OllamaAdapter
from app.providers.openai_adapter import OpenAIAdapter


def _patch_transport(monkeypatch, module_path: str) -> None:
    """
    Redirects `httpx.AsyncClient` inside a given adapter module so every
    call it makes is served in-process by the mock's ASGI app instead of
    going over the network -- the same technique
    tests/unit/test_gemini_provider.py already uses for its own
    MockTransport-based regression test, just pointed at a real app
    instead of a handler function.
    """
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_async_client(*args, transport=httpx.ASGITransport(app=mock_app), **kwargs)

    monkeypatch.setattr(f"{module_path}.httpx.AsyncClient", factory)


def _request(model: str) -> UnifiedChatRequest:
    return UnifiedChatRequest(model=model, messages=[ChatMessage(role="user", content="hi")], max_tokens=64)


# -- OpenAI --------------------------------------------------------------


async def test_openai_adapter_round_trips_against_the_mock(monkeypatch):
    _patch_transport(monkeypatch, "app.providers.openai_adapter")
    adapter = OpenAIAdapter(api_key="unused", base_url="http://mockhost/openai")
    request = _request("openai:mock-gpt")

    payload = adapter.translate_request(request, provider_model="mock-gpt")
    raw = await adapter.call(payload)
    unified = adapter.translate_response(raw, request=request, provider_model="mock-gpt")

    assert unified.provider == "openai"
    assert unified.choices[0].message.content == "This is a mock response from the Phase 5 test double."
    assert unified.usage.input_tokens == 10
    assert unified.usage.output_tokens == 5


async def test_openai_adapter_streams_against_the_mock(monkeypatch):
    _patch_transport(monkeypatch, "app.providers.openai_adapter")
    adapter = OpenAIAdapter(api_key="unused", base_url="http://mockhost/openai")
    request = _request("openai:mock-gpt")
    request.stream = True

    payload = adapter.translate_request(request, provider_model="mock-gpt")
    chunks = [c async for c in adapter.stream(payload, request=request, provider_model="mock-gpt")]

    full_text = "".join(c.delta for c in chunks)
    assert full_text == "This is a mock response."
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.output_tokens == 5


async def test_openai_adapter_surfaces_injected_chaos_as_a_retryable_provider_error(monkeypatch):
    # Driving `chaos` (the same singleton main.py's route handlers close
    # over) directly, in-process, rather than a real HTTP call to
    # /_chaos/config -- everything in this file runs through one Python
    # process via ASGITransport, so there is no live server on port 9000
    # to call. test_full_stack_integration.py exercises the real HTTP
    # control plane against an actual running container instead.
    mock_chaos.set_rule(provider="openai", model="*", error_rate=1.0)
    try:
        _patch_transport(monkeypatch, "app.providers.openai_adapter")
        adapter = OpenAIAdapter(api_key="unused", base_url="http://mockhost/openai")
        request = _request("openai:mock-gpt")
        payload = adapter.translate_request(request, provider_model="mock-gpt")

        with pytest.raises(ProviderError) as exc_info:
            await adapter.call(payload)
        assert exc_info.value.status_code == 503
        assert exc_info.value.retryable is True
    finally:
        mock_chaos.clear()


# -- Anthropic -----------------------------------------------------------


async def test_anthropic_adapter_round_trips_against_the_mock(monkeypatch):
    _patch_transport(monkeypatch, "app.providers.anthropic_adapter")
    adapter = AnthropicAdapter(api_key="unused", base_url="http://mockhost/anthropic")
    request = _request("anthropic:mock-claude")

    payload = adapter.translate_request(request, provider_model="mock-claude")
    raw = await adapter.call(payload)
    unified = adapter.translate_response(raw, request=request, provider_model="mock-claude")

    assert unified.provider == "anthropic"
    assert unified.choices[0].message.content == "This is a mock response from the Phase 5 test double."
    assert unified.choices[0].finish_reason == "stop"
    assert unified.usage.input_tokens == 10
    assert unified.usage.output_tokens == 5


async def test_anthropic_adapter_streams_against_the_mock(monkeypatch):
    _patch_transport(monkeypatch, "app.providers.anthropic_adapter")
    adapter = AnthropicAdapter(api_key="unused", base_url="http://mockhost/anthropic")
    request = _request("anthropic:mock-claude")
    request.stream = True

    payload = adapter.translate_request(request, provider_model="mock-claude")
    chunks = [c async for c in adapter.stream(payload, request=request, provider_model="mock-claude")]

    full_text = "".join(c.delta for c in chunks)
    assert full_text == "This is a mock response."
    terminal = next(c for c in chunks if c.finish_reason is not None)
    assert terminal.finish_reason == "stop"
    assert terminal.usage.output_tokens == 5


# -- Ollama ----------------------------------------------------------------


async def test_ollama_adapter_round_trips_against_the_mock(monkeypatch):
    _patch_transport(monkeypatch, "app.providers.ollama_adapter")
    adapter = OllamaAdapter(base_url="http://mockhost/ollama")
    request = _request("ollama:mock-llama")

    payload = adapter.translate_request(request, provider_model="mock-llama")
    raw = await adapter.call(payload)
    unified = adapter.translate_response(raw, request=request, provider_model="mock-llama")

    assert unified.provider == "ollama"
    assert unified.choices[0].message.content == "This is a mock response from the Phase 5 test double."
    assert unified.usage.input_tokens == 10
    assert unified.usage.output_tokens == 5


async def test_ollama_adapter_streams_ndjson_against_the_mock(monkeypatch):
    _patch_transport(monkeypatch, "app.providers.ollama_adapter")
    adapter = OllamaAdapter(base_url="http://mockhost/ollama")
    request = _request("ollama:mock-llama")
    request.stream = True

    payload = adapter.translate_request(request, provider_model="mock-llama")
    chunks = [c async for c in adapter.stream(payload, request=request, provider_model="mock-llama")]

    full_text = "".join(c.delta for c in chunks)
    assert full_text == "This is a mock response."
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].usage.output_tokens == 5


async def test_ollama_chaos_error_body_is_a_plain_string_not_a_nested_object(monkeypatch):
    """Ollama's real API returns {"error": "<string>"}, not the nested
    {"error": {"message": ...}} shape OpenAI/Anthropic/Gemini use --
    _raise_for_status_bytes in ollama_adapter.py reads it that way. If
    the mock ever sent the nested shape here by mistake, `detail` would
    just be the dict's repr instead of a clean string, and this assertion
    would catch it."""
    mock_chaos.set_rule(provider="ollama", model="*", error_rate=1.0)
    try:
        _patch_transport(monkeypatch, "app.providers.ollama_adapter")
        adapter = OllamaAdapter(base_url="http://mockhost/ollama")
        request = _request("ollama:mock-llama")
        payload = adapter.translate_request(request, provider_model="mock-llama")

        with pytest.raises(ProviderError) as exc_info:
            await adapter.call(payload)
        assert exc_info.value.status_code == 503
        assert "mock chaos injection" in exc_info.value.message
    finally:
        mock_chaos.clear()


# -- Gemini ------------------------------------------------------------------


async def test_gemini_adapter_round_trips_against_the_mock(monkeypatch):
    _patch_transport(monkeypatch, "app.providers.gemini_adapter")
    adapter = GeminiAdapter(api_key="unused", base_url="http://mockhost/gemini/v1beta")
    request = _request("gemini:mock-gemini")

    payload = adapter.translate_request(request, provider_model="mock-gemini")
    raw = await adapter.call(payload)
    unified = adapter.translate_response(raw, request=request, provider_model="mock-gemini")

    assert unified.provider == "gemini"
    assert unified.choices[0].message.content == "This is a mock response from the Phase 5 test double."
    assert unified.usage.input_tokens == 10
    assert unified.usage.output_tokens == 5


# -- chaos latency ------------------------------------------------------------


async def test_chaos_latency_is_applied_without_forcing_an_error(monkeypatch):
    """error_rate=0 with a latency rule must add delay but never raise --
    this is what the k6 P99-SLA scenario and test_health_checking-style
    'provider is slow, not down' cases both need, independent of the
    outage/failover scenarios above."""
    import time

    mock_chaos.set_rule(provider="openai", model="*", latency_ms=120, error_rate=0.0)
    try:
        _patch_transport(monkeypatch, "app.providers.openai_adapter")
        adapter = OpenAIAdapter(api_key="unused", base_url="http://mockhost/openai")
        request = _request("openai:mock-gpt")
        payload = adapter.translate_request(request, provider_model="mock-gpt")

        start = time.monotonic()
        raw = await adapter.call(payload)
        elapsed = time.monotonic() - start

        assert elapsed >= 0.11
        assert raw["output_text"]  # still succeeded, just slow
    finally:
        mock_chaos.clear()
