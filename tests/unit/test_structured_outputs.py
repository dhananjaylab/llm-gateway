"""
test_structured_outputs.py

Phase 8 test plan (docs/PHASE8_KICKOFF_SCOPING.md §7): "json_schema
response format translates correctly for OpenAI/Anthropic/Gemini;
Ollama's format passthrough; a documented, tested error (not silent
degradation) when a request asks for json_schema against a Claude
model/tier not known to support output_config."
"""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from app.core.schema import ChatMessage, ResponseFormat, UnifiedChatRequest
from app.providers.anthropic_adapter import AnthropicAdapter
from app.providers.base import ProviderError
from app.providers.gemini_adapter import GeminiAdapter, _to_gemini_schema
from app.providers.ollama_adapter import OllamaAdapter
from app.providers.openai_adapter import OpenAIAdapter

_SCHEMA = {
    "name": "weather_report",
    "schema": {
        "type": "object",
        "properties": {"temp_f": {"type": "number"}, "city": {"type": "string"}},
        "required": ["temp_f", "city"],
    },
}


def _request(response_format: ResponseFormat) -> UnifiedChatRequest:
    return UnifiedChatRequest(
        model="placeholder:placeholder",
        messages=[ChatMessage(role="user", content="weather?")],
        response_format=response_format,
    )


# -- ResponseFormat validation ------------------------------------------


def test_json_schema_type_requires_a_json_schema_dict():
    with pytest.raises(ValidationError):
        ResponseFormat(type="json_schema", json_schema=None)


def test_json_schema_type_requires_a_schema_key():
    with pytest.raises(ValidationError):
        ResponseFormat(type="json_schema", json_schema={"name": "x"})


def test_json_object_and_text_are_unaffected_by_the_new_validation():
    ResponseFormat(type="json_object")
    ResponseFormat(type="text")


# -- OpenAI -----------------------------------------------------------------


def test_openai_translates_json_schema_to_text_format():
    adapter = OpenAIAdapter(api_key="k")
    req = _request(ResponseFormat(type="json_schema", json_schema=_SCHEMA))
    payload = adapter.translate_request(req, provider_model="gpt-5.6-sol")
    assert payload["text"] == {
        "format": {
            "type": "json_schema",
            "name": "weather_report",
            "schema": _SCHEMA["schema"],
            "strict": True,
        }
    }


def test_openai_json_object_is_unchanged_from_pre_phase_8():
    adapter = OpenAIAdapter(api_key="k")
    req = _request(ResponseFormat(type="json_object"))
    payload = adapter.translate_request(req, provider_model="gpt-5.6-sol")
    assert payload["text"] == {"format": {"type": "json_object"}}


# -- Anthropic: native path + capability fallback ----------------------------


def test_anthropic_translates_json_schema_to_output_config():
    adapter = AnthropicAdapter(api_key="k")
    req = _request(ResponseFormat(type="json_schema", json_schema=_SCHEMA))
    payload = adapter.translate_request(req, provider_model="claude-sonnet-5")
    assert payload["output_config"] == {"format": {"type": "json_schema", "schema": _SCHEMA["schema"]}}


def test_anthropic_json_object_remains_a_no_op_pre_existing_gap():
    """Anthropic has no generic 'valid JSON, any shape' mode — this was
    already true before Phase 8 (translate_request never referenced
    response_format at all); only json_schema is newly handled."""
    adapter = AnthropicAdapter(api_key="k")
    req = _request(ResponseFormat(type="json_object"))
    payload = adapter.translate_request(req, provider_model="claude-sonnet-5")
    assert "output_config" not in payload


def _mock_anthropic_client(handler):
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.pop("timeout", None)
        return real_async_client(*args, transport=httpx.MockTransport(handler), **kwargs)

    return factory


async def test_anthropic_uses_output_config_directly_when_the_model_supports_it(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "msg_1",
                "model": "claude-opus-5",
                "content": [{"type": "text", "text": '{"temp_f": 72, "city": "Pune"}'}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 3},
            },
        )

    monkeypatch.setattr("app.providers.anthropic_adapter.httpx.AsyncClient", _mock_anthropic_client(handler))
    adapter = AnthropicAdapter(api_key="k")
    req = _request(ResponseFormat(type="json_schema", json_schema=_SCHEMA))
    payload = adapter.translate_request(req, provider_model="claude-opus-5")

    raw = await adapter.call(payload)
    unified = adapter.translate_response(raw, request=req, provider_model="claude-opus-5")

    assert len(calls) == 1, "no fallback should have run — the model accepted output_config"
    assert "output_config" in calls[0]
    assert json.loads(unified.choices[0].message.content) == {"temp_f": 72, "city": "Pune"}
    assert unified.choices[0].message.tool_calls is None
    await adapter.aclose()


async def test_anthropic_falls_back_to_a_forced_tool_call_when_output_config_is_unsupported(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if "output_config" in body:
            return httpx.Response(
                400,
                json={
                    "error": {
                        "type": "invalid_request_error",
                        "message": "output_config is not supported for model claude-haiku-4-5",
                    }
                },
            )
        assert body["tool_choice"] == {"type": "tool", "name": "__gateway_structured_output__"}
        return httpx.Response(
            200,
            json={
                "id": "msg_2",
                "model": "claude-haiku-4-5",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_fb",
                        "name": "__gateway_structured_output__",
                        "input": {"temp_f": 72, "city": "Pune"},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 4, "output_tokens": 2},
            },
        )

    monkeypatch.setattr("app.providers.anthropic_adapter.httpx.AsyncClient", _mock_anthropic_client(handler))
    adapter = AnthropicAdapter(api_key="k")
    req = _request(ResponseFormat(type="json_schema", json_schema=_SCHEMA))
    payload = adapter.translate_request(req, provider_model="claude-haiku-4-5")

    raw = await adapter.call(payload)
    unified = adapter.translate_response(raw, request=req, provider_model="claude-haiku-4-5")

    assert len(calls) == 2, "exactly one automatic retry, not a loop"
    # The client sees a normal schema-shaped text answer -- no visible
    # sign the sentinel-tool fallback ran, and no leaked ToolCall for a
    # tool the client never defined.
    assert json.loads(unified.choices[0].message.content) == {"temp_f": 72, "city": "Pune"}
    assert unified.choices[0].message.tool_calls is None
    assert unified.choices[0].finish_reason == "stop"
    await adapter.aclose()


async def test_anthropic_does_not_fall_back_on_an_unrelated_400(monkeypatch):
    """A genuinely malformed request (not an output_config-support issue)
    must surface as a normal ProviderError, not trigger the fallback."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"error": {"type": "invalid_request_error", "message": "max_tokens is required"}}
        )

    monkeypatch.setattr("app.providers.anthropic_adapter.httpx.AsyncClient", _mock_anthropic_client(handler))
    adapter = AnthropicAdapter(api_key="k")
    req = _request(ResponseFormat(type="json_schema", json_schema=_SCHEMA))
    payload = adapter.translate_request(req, provider_model="claude-haiku-4-5")

    with pytest.raises(ProviderError) as exc_info:
        await adapter.call(payload)
    assert exc_info.value.retryable is False
    await adapter.aclose()


async def test_anthropic_fallback_is_not_triggered_when_output_config_was_never_sent(monkeypatch):
    """A plain (non-structured-output) 400 must never be routed through
    the fallback path just because it happens to be a 400."""
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(
            400, json={"error": {"type": "invalid_request_error", "message": "bad request"}}
        )

    monkeypatch.setattr("app.providers.anthropic_adapter.httpx.AsyncClient", _mock_anthropic_client(handler))
    adapter = AnthropicAdapter(api_key="k")
    payload = adapter.translate_request(
        UnifiedChatRequest(
            model="anthropic:claude-sonnet-5", messages=[ChatMessage(role="user", content="hi")]
        ),
        provider_model="claude-sonnet-5",
    )
    with pytest.raises(ProviderError):
        await adapter.call(payload)
    assert len(calls) == 1
    await adapter.aclose()


# -- Gemini -------------------------------------------------------------


def test_gemini_translates_json_schema_to_response_schema():
    adapter = GeminiAdapter(api_key="k")
    req = _request(ResponseFormat(type="json_schema", json_schema=_SCHEMA))
    payload = adapter.translate_request(req, provider_model="gemini-3.6-flash")
    assert payload["generationConfig"]["responseMimeType"] == "application/json"
    assert payload["generationConfig"]["responseSchema"]["type"] == "OBJECT"
    assert payload["generationConfig"]["responseSchema"]["properties"]["temp_f"]["type"] == "NUMBER"


def test_gemini_schema_case_conversion_recurses_into_nested_array_and_object():
    schema = {
        "type": "object",
        "properties": {
            "items": {"type": "array", "items": {"type": "string"}},
            "count": {"type": "integer"},
            "flag": {"type": "boolean"},
        },
    }
    converted = _to_gemini_schema(schema)
    assert converted["type"] == "OBJECT"
    assert converted["properties"]["items"]["type"] == "ARRAY"
    assert converted["properties"]["items"]["items"]["type"] == "STRING"
    assert converted["properties"]["count"]["type"] == "INTEGER"
    assert converted["properties"]["flag"]["type"] == "BOOLEAN"


@pytest.mark.parametrize("keyword", ["oneOf", "anyOf", "allOf", "$ref"])
def test_gemini_raises_a_clear_error_for_unsupported_schema_keywords(keyword):
    schema = {keyword: [{"type": "string"}]} if keyword != "$ref" else {"$ref": "#/defs/x"}
    with pytest.raises(ProviderError) as exc_info:
        _to_gemini_schema(schema)
    assert exc_info.value.error_type == "unsupported_schema_feature"
    assert exc_info.value.retryable is False


# -- Ollama -------------------------------------------------------------


def test_ollama_translates_json_schema_to_native_format_field():
    adapter = OllamaAdapter()
    req = _request(ResponseFormat(type="json_schema", json_schema=_SCHEMA))
    payload = adapter.translate_request(req, provider_model="llama3.2")
    assert payload["format"] == _SCHEMA["schema"]


def test_ollama_translates_json_object_to_legacy_json_string():
    adapter = OllamaAdapter()
    req = _request(ResponseFormat(type="json_object"))
    payload = adapter.translate_request(req, provider_model="llama3.2")
    assert payload["format"] == "json"


def test_ollama_no_response_format_means_no_format_key():
    adapter = OllamaAdapter()
    req = UnifiedChatRequest(model="ollama:llama3.2", messages=[ChatMessage(role="user", content="hi")])
    payload = adapter.translate_request(req, provider_model="llama3.2")
    assert "format" not in payload
