"""
test_error_classification.py

Verifies the Phase 3 test plan: "Each documented retryable/non-retryable
error code routes to the correct branch (retry vs. immediate bubble-up)."

Two layers:
- Adapter level: every adapter's `_raise_for_status_bytes` maps the TRD's
  documented status codes (429/502/503/504 retryable; 400/401/403 not)
  onto `ProviderError.retryable` correctly. This was never actually
  pinned down by a test in Phase 1/2 — `ProviderError.retryable` existed
  since Phase 1 but nothing asserted its value until Phase 3 needed to
  build real behavior on top of it.
- Router level: `FallbackRouter` (via the full HTTP pipeline) actually
  branches on that flag the way Document 03 specifies — retryable exhausts
  its attempts then advances the chain, non-retryable aborts the whole
  walk immediately. This overlaps in spirit with
  test_fallback_chain.py's non-retryable test, but parametrizes across
  every documented code rather than checking one.
"""

from __future__ import annotations

import httpx
import pytest

from app.providers.anthropic_adapter import AnthropicAdapter
from app.providers.base import ProviderError
from app.providers.gemini_adapter import GeminiAdapter
from app.providers.ollama_adapter import OllamaAdapter
from app.providers.openai_adapter import OpenAIAdapter
from tests.unit.conftest import DATA_SCIENCE_KEY, FakeAdapter, running_app_client

_RETRYABLE_CODES = [429, 502, 503, 504]
_NON_RETRYABLE_CODES = [400, 401, 403]


def _mock_client_returning(status_code: int, body: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=body)

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_async_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    return fake_async_client


# -- OpenAI ------------------------------------------------------------------


@pytest.mark.parametrize("status_code", _RETRYABLE_CODES)
async def test_openai_retryable_status_codes(monkeypatch, status_code):
    monkeypatch.setattr(
        "app.providers.openai_adapter.httpx.AsyncClient",
        _mock_client_returning(status_code, {"error": {"message": "boom"}}),
    )
    adapter = OpenAIAdapter(api_key="sk-test")
    with pytest.raises(ProviderError) as exc_info:
        await adapter.call({"model": "gpt-5.4", "messages": []})
    assert exc_info.value.retryable is True
    assert exc_info.value.status_code == status_code


@pytest.mark.parametrize("status_code", _NON_RETRYABLE_CODES)
async def test_openai_non_retryable_status_codes(monkeypatch, status_code):
    monkeypatch.setattr(
        "app.providers.openai_adapter.httpx.AsyncClient",
        _mock_client_returning(status_code, {"error": {"message": "bad"}}),
    )
    adapter = OpenAIAdapter(api_key="sk-test")
    with pytest.raises(ProviderError) as exc_info:
        await adapter.call({"model": "gpt-5.4", "messages": []})
    assert exc_info.value.retryable is False
    assert exc_info.value.status_code == status_code


# -- Anthropic -----------------------------------------------------------


@pytest.mark.parametrize("status_code", _RETRYABLE_CODES)
async def test_anthropic_retryable_status_codes(monkeypatch, status_code):
    monkeypatch.setattr(
        "app.providers.anthropic_adapter.httpx.AsyncClient",
        _mock_client_returning(status_code, {"error": {"message": "boom"}}),
    )
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    with pytest.raises(ProviderError) as exc_info:
        await adapter.call({"model": "claude-sonnet-5", "messages": [], "max_tokens": 16})
    assert exc_info.value.retryable is True


@pytest.mark.parametrize("status_code", _NON_RETRYABLE_CODES)
async def test_anthropic_non_retryable_status_codes(monkeypatch, status_code):
    monkeypatch.setattr(
        "app.providers.anthropic_adapter.httpx.AsyncClient",
        _mock_client_returning(status_code, {"error": {"message": "bad"}}),
    )
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    with pytest.raises(ProviderError) as exc_info:
        await adapter.call({"model": "claude-sonnet-5", "messages": [], "max_tokens": 16})
    assert exc_info.value.retryable is False


# -- Ollama ------------------------------------------------------------------


@pytest.mark.parametrize("status_code", _RETRYABLE_CODES)
async def test_ollama_retryable_status_codes(monkeypatch, status_code):
    monkeypatch.setattr(
        "app.providers.ollama_adapter.httpx.AsyncClient",
        _mock_client_returning(status_code, {"error": "boom"}),
    )
    adapter = OllamaAdapter()
    with pytest.raises(ProviderError) as exc_info:
        await adapter.call({"model": "llama3.2", "messages": []})
    assert exc_info.value.retryable is True


@pytest.mark.parametrize("status_code", _NON_RETRYABLE_CODES)
async def test_ollama_non_retryable_status_codes(monkeypatch, status_code):
    monkeypatch.setattr(
        "app.providers.ollama_adapter.httpx.AsyncClient",
        _mock_client_returning(status_code, {"error": "bad"}),
    )
    adapter = OllamaAdapter()
    with pytest.raises(ProviderError) as exc_info:
        await adapter.call({"model": "llama3.2", "messages": []})
    assert exc_info.value.retryable is False


# -- Gemini ------------------------------------------------------------------


@pytest.mark.parametrize("status_code", _RETRYABLE_CODES)
async def test_gemini_retryable_status_codes(monkeypatch, status_code):
    monkeypatch.setattr(
        "app.providers.gemini_adapter.httpx.AsyncClient",
        _mock_client_returning(status_code, {"error": {"message": "boom"}}),
    )
    adapter = GeminiAdapter(api_key="test-key")
    with pytest.raises(ProviderError) as exc_info:
        await adapter.call({"model": "gemini-3.6-flash", "contents": []})
    assert exc_info.value.retryable is True


@pytest.mark.parametrize("status_code", _NON_RETRYABLE_CODES)
async def test_gemini_non_retryable_status_codes(monkeypatch, status_code):
    monkeypatch.setattr(
        "app.providers.gemini_adapter.httpx.AsyncClient",
        _mock_client_returning(status_code, {"error": {"message": "bad"}}),
    )
    adapter = GeminiAdapter(api_key="test-key")
    with pytest.raises(ProviderError) as exc_info:
        await adapter.call({"model": "gemini-3.6-flash", "contents": []})
    assert exc_info.value.retryable is False


# -- router-level: the branch each classification actually drives ------------


@pytest.mark.parametrize("status_code", _RETRYABLE_CODES)
async def test_router_retries_then_falls_back_for_every_retryable_code(app, monkeypatch, status_code):
    failing = FakeAdapter(always_fail=True, retryable=True, status_code=status_code, error_type="x")
    working = FakeAdapter(response_text="ok from fallback")

    def _resolve(model_id: str):
        if model_id == "openai:gpt-5.4":
            return failing, "gpt-5.4-served"
        if model_id == "anthropic:claude-sonnet-5":
            return working, "claude-sonnet-5-served"
        raise AssertionError(model_id)

    monkeypatch.setattr("app.api.v1_chat.resolve_model", _resolve)

    async with running_app_client(app) as client:
        await app.state.team_store.update_team("data-science", {"allowed_models": ["tier-1-reasoning"]})
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "tier-1-reasoning", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        )

    assert resp.status_code == 200
    assert failing.call_count == 3  # RETRY_MAX_ATTEMPTS (conftest.py)
    assert working.call_count == 1


@pytest.mark.parametrize("status_code", _NON_RETRYABLE_CODES)
async def test_router_aborts_immediately_for_every_non_retryable_code(app, monkeypatch, status_code):
    failing = FakeAdapter(always_fail=True, retryable=False, status_code=status_code, error_type="x")
    working = FakeAdapter(response_text="must never be reached")

    def _resolve(model_id: str):
        if model_id == "openai:gpt-5.4":
            return failing, "gpt-5.4-served"
        raise AssertionError(f"non-retryable code {status_code} must not advance to {model_id}")

    monkeypatch.setattr("app.api.v1_chat.resolve_model", _resolve)

    async with running_app_client(app) as client:
        await app.state.team_store.update_team("data-science", {"allowed_models": ["tier-1-reasoning"]})
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "tier-1-reasoning", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        )

    assert resp.status_code == 502
    assert failing.call_count == 1
    assert working.call_count == 0
