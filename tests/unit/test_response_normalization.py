"""
test_response_normalization.py

Verifies: "All three providers' distinct response shapes normalize back to
one schema (content[0].text vs. choices[0].message.content, etc.)" — per
the Phase 1 test plan. Fixture payloads below mirror each provider's real
non-streaming response shape.
"""

from __future__ import annotations

from app.core.schema import ChatMessage, UnifiedChatRequest
from app.providers.anthropic_adapter import AnthropicAdapter
from app.providers.ollama_adapter import OllamaAdapter
from app.providers.openai_adapter import OpenAIAdapter


def _request(model: str) -> UnifiedChatRequest:
    return UnifiedChatRequest(model=model, messages=[ChatMessage(role="user", content="hi")])


def test_openai_response_normalizes_output_text_shorthand():
    """output_text string shorthand is the fast path for single-output responses."""
    raw = {
        "id": "resp_abc123",
        "object": "response",
        "created_at": 1730000000,
        "model": "gpt-5.6-sol",
        "output_text": "Token buckets refill over time.",
        "output": [
            {
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Token buckets refill over time."}],
            }
        ],
        "usage": {
            "input_tokens": 12,
            "output_tokens": 8,
            "input_tokens_details": {"cached_tokens": 0},
        },
    }
    adapter = OpenAIAdapter(api_key="sk-test")
    unified = adapter.translate_response(
        raw, request=_request("openai:gpt-5.6-sol"), provider_model="gpt-5.6-sol"
    )

    assert unified.provider == "openai"
    assert unified.model_served == "gpt-5.6-sol"
    assert unified.choices[0].message.content == "Token buckets refill over time."
    assert unified.choices[0].finish_reason == "stop"
    assert unified.usage.input_tokens == 12
    assert unified.usage.output_tokens == 8
    assert unified.created == 1730000000


def test_openai_response_normalizes_typed_output_items():
    """Typed output[].content[] path is used when output_text is absent."""
    raw = {
        "id": "resp_def456",
        "object": "response",
        "created_at": 1730000001,
        "model": "gpt-5.6-sol",
        "output": [
            {
                "type": "message",
                "status": "completed",
                "content": [
                    {"type": "output_text", "text": "Part one. "},
                    {"type": "output_text", "text": "Part two."},
                ],
            }
        ],
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "input_tokens_details": {"cached_tokens": 0},
        },
    }
    adapter = OpenAIAdapter(api_key="sk-test")
    unified = adapter.translate_response(
        raw, request=_request("openai:gpt-5.6-sol"), provider_model="gpt-5.6-sol"
    )
    assert unified.choices[0].message.content == "Part one. Part two."
    assert unified.choices[0].finish_reason == "stop"


def test_openai_response_maps_cached_tokens():
    """input_tokens_details.cached_tokens maps to cache_read_input_tokens."""
    raw = {
        "id": "resp_cache",
        "object": "response",
        "created_at": 1730000002,
        "model": "gpt-5.6-sol",
        "output_text": "cached response",
        "output": [
            {
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": "cached response"}],
            }
        ],
        "usage": {
            "input_tokens": 20,
            "output_tokens": 4,
            "input_tokens_details": {"cached_tokens": 15},
        },
    }
    adapter = OpenAIAdapter(api_key="sk-test")
    unified = adapter.translate_response(
        raw, request=_request("openai:gpt-5.6-sol"), provider_model="gpt-5.6-sol"
    )
    assert unified.usage.cache_read_input_tokens == 15
    assert unified.usage.input_tokens == 20


def test_openai_response_uses_created_at_as_timestamp():
    """created_at from the Responses API becomes the normalized created field."""
    raw = {
        "id": "resp_ts",
        "object": "response",
        "created_at": 1730099999,
        "model": "gpt-5.6-sol",
        "output_text": "hi",
        "output": [
            {
                "type": "message",
                "status": "completed",
                "content": [{"type": "output_text", "text": "hi"}],
            }
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
    adapter = OpenAIAdapter(api_key="sk-test")
    unified = adapter.translate_response(
        raw, request=_request("openai:gpt-5.6-sol"), provider_model="gpt-5.6-sol"
    )
    assert unified.created == 1730099999


def test_anthropic_response_normalizes_content_block_text():
    raw = {
        "id": "msg_01abc",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5",
        "content": [{"type": "text", "text": "Token buckets refill over time."}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 12,
            "output_tokens": 8,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        },
    }
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    unified = adapter.translate_response(
        raw, request=_request("anthropic:claude-sonnet-5"), provider_model="claude-sonnet-5"
    )

    assert unified.provider == "anthropic"
    assert unified.model_served == "claude-sonnet-5"
    assert unified.choices[0].message.content == "Token buckets refill over time."
    assert unified.choices[0].finish_reason == "stop"
    assert unified.usage.input_tokens == 12
    assert unified.usage.output_tokens == 8


def test_anthropic_multiple_text_blocks_are_concatenated():
    raw = {
        "id": "msg_02abc",
        "model": "claude-sonnet-5",
        "content": [
            {"type": "text", "text": "Part one. "},
            {"type": "text", "text": "Part two."},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 5, "output_tokens": 5},
    }
    adapter = AnthropicAdapter(api_key="sk-ant-test")
    unified = adapter.translate_response(
        raw, request=_request("anthropic:claude-sonnet-5"), provider_model="claude-sonnet-5"
    )
    assert unified.choices[0].message.content == "Part one. Part two."


def test_ollama_response_normalizes_message_content_and_eval_counts():
    raw = {
        "model": "llama3.2",
        "created_at": "2026-08-02T00:00:00Z",
        "message": {"role": "assistant", "content": "Token buckets refill over time."},
        "done": True,
        "done_reason": "stop",
        "prompt_eval_count": 12,
        "eval_count": 8,
    }
    adapter = OllamaAdapter(base_url="http://localhost:11434")
    unified = adapter.translate_response(
        raw, request=_request("ollama:llama3.2"), provider_model="llama3.2"
    )

    assert unified.provider == "ollama"
    assert unified.model_served == "llama3.2"
    assert unified.choices[0].message.content == "Token buckets refill over time."
    assert unified.choices[0].finish_reason == "stop"
    assert unified.usage.input_tokens == 12
    assert unified.usage.output_tokens == 8


def test_ollama_response_mints_an_id_since_ollama_has_none():
    raw = {
        "model": "llama3.2",
        "message": {"role": "assistant", "content": "hi"},
        "done": True,
        "prompt_eval_count": 1,
        "eval_count": 1,
    }
    adapter = OllamaAdapter()
    unified = adapter.translate_response(
        raw, request=_request("ollama:llama3.2"), provider_model="llama3.2"
    )
    assert unified.id.startswith("gw-")


def test_all_three_providers_produce_the_same_response_shape():
    openai_unified = OpenAIAdapter(api_key="k").translate_response(
        {
            "id": "resp_1",
            "model": "gpt-5.6-sol",
            "output_text": "x",
            "output": [
                {
                    "type": "message",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "x"}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        request=_request("openai:gpt-5.6-sol"),
        provider_model="gpt-5.6-sol",
    )
    anthropic_unified = AnthropicAdapter(api_key="k").translate_response(
        {
            "id": "2",
            "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": "x"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        },
        request=_request("anthropic:claude-sonnet-5"),
        provider_model="claude-sonnet-5",
    )
    ollama_unified = OllamaAdapter().translate_response(
        {
            "model": "llama3.2",
            "message": {"content": "x"},
            "done": True,
            "prompt_eval_count": 1,
            "eval_count": 1,
        },
        request=_request("ollama:llama3.2"),
        provider_model="llama3.2",
    )

    assert {type(r) for r in (openai_unified, anthropic_unified, ollama_unified)} == {
        type(openai_unified)
    }
    for unified in (openai_unified, anthropic_unified, ollama_unified):
        assert unified.choices[0].message.content == "x"
        assert unified.choices[0].finish_reason == "stop"
        assert unified.usage.total_tokens == 2
