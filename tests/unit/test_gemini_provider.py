from __future__ import annotations

import json

import httpx
import pytest

from app.core import config as config_module
from app.core.schema import ChatMessage, UnifiedChatRequest
from app.providers.base import ProviderError
from app.providers.gemini_adapter import GeminiAdapter
from app.providers import registry as registry_module
from tests.unit.conftest import DATA_SCIENCE_KEY


def test_gemini_adapter_translates_request_and_response():
    adapter = GeminiAdapter(api_key="test-gemini-key")
    request = UnifiedChatRequest(
        model="gemini:gemini-2.0-flash",
        system="You are helpful",
        messages=[ChatMessage(role="user", content="hi")],
        max_tokens=16,
        temperature=0.7,
        top_p=0.9,
        stop=["END"],
        stream=False,
    )

    payload = adapter.translate_request(request, provider_model="gemini-2.0-flash")

    assert payload["model"] == "gemini-2.0-flash"
    assert payload["systemInstruction"]["parts"][0]["text"] == "You are helpful"
    assert payload["generationConfig"]["maxOutputTokens"] == 16
    assert payload["generationConfig"]["temperature"] == 0.7
    assert payload["generationConfig"]["topP"] == 0.9
    assert payload["generationConfig"]["stopSequences"] == ["END"]
    assert payload["contents"][0]["parts"][0]["text"] == "hi"

    raw = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "hello from gemini"}],
                    "role": "model",
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 4,
            "candidatesTokenCount": 3,
            "totalTokenCount": 7,
        },
    }

    response = adapter.translate_response(raw, request=request, provider_model="gemini-2.0-flash")

    assert response.provider == "gemini"
    assert response.choices[0].message.content == "hello from gemini"
    assert response.usage.input_tokens == 4
    assert response.usage.output_tokens == 3


def test_registry_registers_gemini_when_key_is_set(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    registry_module.reset_registry_cache()

    adapter, provider_model = registry_module.resolve_model("gemini:gemini-2.0-flash")

    assert adapter.provider_name == "gemini"
    assert provider_model == "gemini-2.0-flash"


def test_provider_settings_load_from_dotenv(monkeypatch, tmp_path):
    """
    Regression test for a flaky original version of this test: it used to
    delete GEMINI_API_KEY from the environment and assert the loaded value
    "is not None," which only passed if a real, untracked .env with a live
    key happened to exist on the machine running the test — it failed on
    every clean checkout (confirmed: 36 passed / 1 failed on a fresh clone
    with no local .env).

    Fixed version: point config_module._PROJECT_ROOT at a temp directory
    containing a throwaway .env, and assert against the *exact* value that
    file declares. This proves dotenv loading actually works, is fully
    reproducible in CI, and never touches a real credential.
    """
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    (tmp_path / ".env").write_text("GEMINI_API_KEY=from-dotenv-test-key\n")
    monkeypatch.setattr(config_module, "_PROJECT_ROOT", tmp_path)
    config_module.reset_provider_settings_cache()

    settings = config_module.get_provider_settings()

    assert settings.gemini_api_key == "from-dotenv-test-key"


async def test_gemini_stream_raises_a_clean_provider_error_not_a_type_error():
    """
    Regression test for the coroutine-vs-async-generator bug: stream() had
    no `yield` anywhere in its body, so calling it returned a plain
    coroutine rather than an async generator. `async for chunk in
    adapter.stream(...)` then failed with `TypeError: 'async for' requires
    an object with __aiter__ method, got coroutine` instead of the intended
    ProviderError — and a client hitting the real endpoint got a silent,
    empty `200 OK` instead of any error at all (confirmed against the
    unfixed version). The unreachable `yield` added to stream() fixes this;
    this test proves the fix by asserting the returned object is a genuine
    async generator and that iterating it raises the intended ProviderError
    with the expected status/type, not a TypeError.
    """
    adapter = GeminiAdapter(api_key="test-gemini-key")
    request = UnifiedChatRequest(
        model="gemini:gemini-2.0-flash",
        messages=[ChatMessage(role="user", content="hi")],
        stream=True,
    )
    payload = adapter.translate_request(request, provider_model="gemini-2.0-flash")

    gen = adapter.stream(payload, request=request, provider_model="gemini-2.0-flash")
    assert hasattr(gen, "__anext__"), "stream() must return a real async generator, not a coroutine"

    with pytest.raises(ProviderError) as exc_info:
        async for _ in gen:
            pass
    assert exc_info.value.status_code == 501
    assert exc_info.value.error_type == "unsupported_streaming"


def test_gemini_streaming_request_surfaces_a_structured_error_not_silence(client):
    """
    End-to-end confirmation through the real endpoint. Note the status code
    is still 200: StreamingResponse commits headers before the body
    generator runs, so *no* mid-stream provider failure (Gemini's or any
    other provider's) can change the top-level HTTP status — that's true
    even for OpenAI/Anthropic/Ollama and is correct SSE behavior, not a bug.
    What the fix actually changes is the body: previously empty and silent;
    now a structured `event: error` frame the client can act on.
    """
    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": "gemini:gemini-3.6-flash",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )
    assert resp.status_code == 200
    assert "event: error" in resp.text
    assert "unsupported_streaming" in resp.text


async def test_gemini_call_does_not_leak_model_field_into_request_body(monkeypatch):
    """
    Regression test for the stray `model` key: translate_request() embeds
    `model` in the payload dict only so call() can read it back out to
    build the URL (Gemini addresses the model via the path, unlike the
    other three adapters) — but the original call() then POSTed that same
    dict verbatim, leaking an undocumented `model` field into the JSON
    body. This asserts the outgoing request body no longer contains it.

    Uses httpx's own built-in MockTransport rather than respx: respx 0.21.1
    against httpx 0.28.1 fails to match *any* URL/host-based route in this
    environment (confirmed directly — even a bare host-only pattern
    doesn't match, while an unconstrained route mocks fine), so it isn't a
    reliable tool to reach for here yet. MockTransport needs no extra
    dependency and no URL-pattern matching at all.
    """
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": "hi back"}]}, "finishReason": "STOP"}
                ],
                "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1},
            },
        )

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_async_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("app.providers.gemini_adapter.httpx.AsyncClient", fake_async_client)

    adapter = GeminiAdapter(api_key="test-gemini-key")
    payload = {
        "model": "gemini-2.0-flash",
        "contents": [{"role": "user", "parts": [{"text": "hi"}]}],
        "generationConfig": {"maxOutputTokens": 10},
    }

    result = await adapter.call(payload)

    assert "model" not in captured["body"]
    assert captured["body"]["contents"][0]["parts"][0]["text"] == "hi"
    assert captured["url"].endswith("/models/gemini-2.0-flash:generateContent")
    assert result["candidates"][0]["content"]["parts"][0]["text"] == "hi back"
