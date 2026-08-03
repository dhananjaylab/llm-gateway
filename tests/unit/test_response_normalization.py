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


def test_openai_response_normalizes_choices_message_content():
    raw = {
        "id": "chatcmpl-abc123",
        "object": "chat.completion",
        "created": 1730000000,
        "model": "gpt-5.4-2026-03-05",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "Token buckets refill over time."},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 8,
            "total_tokens": 20,
            "prompt_tokens_details": {"cached_tokens": 0},
        },
    }
    adapter = OpenAIAdapter(api_key="sk-test")
    unified = adapter.translate_response(
        raw, request=_request("openai:gpt-5.4"), provider_model="gpt-5.4"
    )

    assert unified.provider == "openai"
    assert unified.model_served == "gpt-5.4-2026-03-05"
    assert unified.choices[0].message.content == "Token buckets refill over time."
    assert unified.choices[0].finish_reason == "stop"
    assert unified.usage.input_tokens == 12
    assert unified.usage.output_tokens == 8


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
    # Anthropic's "end_turn" maps to the unified schema's "stop".
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
    """The whole point: a caller can't tell which provider served the request
    from the shape of the response alone."""
    openai_unified = OpenAIAdapter(api_key="k").translate_response(
        {
            "id": "1",
            "model": "gpt-5.4",
            "choices": [{"message": {"content": "x"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        },
        request=_request("openai:gpt-5.4"),
        provider_model="gpt-5.4",
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
