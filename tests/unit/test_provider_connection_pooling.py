"""
test_provider_connection_pooling.py

Phase 7 regression guard for the connection-pooling bug found during the
external architecture review: every adapter used to do `async with
httpx.AsyncClient(...) as client:` INSIDE call()/stream() -- a brand-new
client (and therefore a brand-new TCP connection pool) constructed and
torn down on every single request. This is the concrete root cause behind
"socket exhaustion (TIME_WAIT) at 5,000+ RPS" -- see
docs/PHASE7_IMPLEMENTATION_GUIDE.md.

This asserts the actual fix: httpx.AsyncClient is constructed exactly
ONCE per adapter instance (at __init__ time), and N concurrent calls
against that same adapter instance all reuse it -- not once per call.
Uses the same MockTransport-substitution technique
test_gemini_provider.py and test_error_classification.py already
established, just wrapped in a counter so the test can assert on
construction *count*, not just correctness of a single call.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from app.providers.anthropic_adapter import AnthropicAdapter
from app.providers.base import ProviderAdapter
from app.providers.gemini_adapter import GeminiAdapter
from app.providers.ollama_adapter import OllamaAdapter
from app.providers.openai_adapter import OpenAIAdapter

_REAL_ASYNC_CLIENT = httpx.AsyncClient  # captured before any monkeypatching below


class _ConstructionCounter:
    """Wraps httpx.AsyncClient so a test can assert exactly how many times
    it was constructed, while every constructed instance still behaves
    like a real client (MockTransport, no real network).

    Builds via `_REAL_ASYNC_CLIENT`, captured at import time -- NOT via a
    bare `httpx.AsyncClient(...)` call inside this method, which would
    resolve to the *patched* attribute (module attributes are looked up
    fresh at call time, and `app.providers.<x>.httpx` is the exact same
    shared `httpx` module object this test file imports) and recurse into
    itself. Same technique test_mock_provider_wire_compat.py's
    `_patch_transport` already uses.
    """

    def __init__(self, response_json: dict) -> None:
        self.construction_count = 0
        self._response_json = response_json

    def factory(self, *args, **kwargs):
        self.construction_count += 1
        kwargs.pop("timeout", None)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=self._response_json)

        return _REAL_ASYNC_CLIENT(*args, transport=httpx.MockTransport(handler), **kwargs)


_MINIMAL_RESPONSES = {
    "openai": {"id": "resp_1", "model": "mock", "output_text": "ok", "output": [], "usage": {}},
    "anthropic": {
        "id": "msg_1",
        "model": "mock",
        "content": [{"type": "text", "text": "ok"}],
        "usage": {},
    },
    "ollama": {"model": "mock", "message": {"content": "ok"}, "done": True},
    "gemini": {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
        "usageMetadata": {},
    },
}

_PAYLOAD = {"model": "mock", "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.parametrize(
    "provider_key,module_path,build_adapter",
    [
        ("openai", "app.providers.openai_adapter", lambda: OpenAIAdapter(api_key="k")),
        ("anthropic", "app.providers.anthropic_adapter", lambda: AnthropicAdapter(api_key="k")),
        ("ollama", "app.providers.ollama_adapter", lambda: OllamaAdapter()),
        ("gemini", "app.providers.gemini_adapter", lambda: GeminiAdapter(api_key="k")),
    ],
    ids=["openai", "anthropic", "ollama", "gemini"],
)
async def test_adapter_constructs_the_http_client_exactly_once_not_per_call(
    monkeypatch, provider_key, module_path, build_adapter
):
    counter = _ConstructionCounter(_MINIMAL_RESPONSES[provider_key])
    monkeypatch.setattr(f"{module_path}.httpx.AsyncClient", counter.factory)

    adapter = build_adapter()
    assert counter.construction_count == 1, (
        "the client must be built once, at __init__ time -- not lazily deferred to the first call "
        "(deferring it would still pass a naive 'only one client ever' check without actually "
        "proving __init__ itself is where construction happens)"
    )

    responses = await asyncio.gather(*[adapter.call(dict(_PAYLOAD)) for _ in range(20)])

    assert len(responses) == 20
    assert counter.construction_count == 1, (
        "20 concurrent calls against the same adapter must all reuse the one client built in "
        "__init__ -- constructing a new httpx.AsyncClient (and therefore a new TCP connection "
        "pool) per call is exactly the socket-exhaustion bug this test guards against"
    )

    await adapter.aclose()


async def test_two_separate_adapter_instances_each_get_their_own_client(monkeypatch):
    """Sanity check on the other direction: pooling is per-adapter-instance,
    not an accidental global singleton that would leak one team's provider
    credentials/base_url into another adapter object."""
    counter = _ConstructionCounter(_MINIMAL_RESPONSES["openai"])
    monkeypatch.setattr("app.providers.openai_adapter.httpx.AsyncClient", counter.factory)

    first = OpenAIAdapter(api_key="k1")
    second = OpenAIAdapter(api_key="k2")

    assert counter.construction_count == 2
    assert first._client is not second._client

    await first.aclose()
    await second.aclose()


def test_aclose_is_a_no_op_default_on_the_base_interface():
    """FakeAdapter (tests/unit/conftest.py) holds no real transport and
    doesn't override aclose() -- the base class's default must not raise,
    so app/providers/registry.py::close_all_adapters() can call it
    unconditionally across a mix of real and (in tests) fake adapters."""

    class _Bare(ProviderAdapter):
        provider_name = "bare"

        def translate_request(self, request, *, provider_model):
            return {}

        async def call(self, payload):
            return {}

        def translate_response(self, raw, *, request, provider_model):
            raise NotImplementedError

        async def stream(self, payload, *, request, provider_model):
            return
            yield  # pragma: no cover - unreachable, makes this a real async generator

    async def _run():
        await _Bare().aclose()

    asyncio.run(_run())
