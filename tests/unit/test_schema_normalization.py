"""
test_schema_normalization.py

Verifies: "A single unified request translates correctly to all three
provider payload shapes (system prompt placement, max_tokens key, stop
sequences)" — per the Phase 1 test plan in the project documentation.
"""

from __future__ import annotations

from app.core.schema import ChatMessage, UnifiedChatRequest
from app.providers.anthropic_adapter import AnthropicAdapter
from app.providers.ollama_adapter import OllamaAdapter
from app.providers.openai_adapter import OpenAIAdapter


def _unified_request(**overrides) -> UnifiedChatRequest:
    defaults = {
        "model": "placeholder:placeholder",
        "system": "You are a helpful enterprise assistant.",
        "messages": [
            ChatMessage(role="user", content="What is a token bucket?"),
        ],
        "max_tokens": 256,
        "temperature": 0.4,
        "stop": ["\n\nEND"],
    }
    defaults.update(overrides)
    return UnifiedChatRequest(**defaults)


class TestOpenAITranslation:
    def setup_method(self):
        self.adapter = OpenAIAdapter(api_key="sk-test")

    def test_system_prompt_becomes_first_message(self):
        payload = self.adapter.translate_request(_unified_request(), provider_model="gpt-5.4")
        assert payload["messages"][0] == {
            "role": "system",
            "content": "You are a helpful enterprise assistant.",
        }
        assert payload["messages"][1]["role"] == "user"

    def test_max_tokens_key_is_max_completion_tokens(self):
        payload = self.adapter.translate_request(_unified_request(), provider_model="gpt-5.4")
        assert payload["max_completion_tokens"] == 256
        assert "max_tokens" not in payload

    def test_stop_sequences_are_not_forwarded_gpt5_rejects_the_param(self):
        payload = self.adapter.translate_request(_unified_request(), provider_model="gpt-5.4")
        assert "stop" not in payload

    def test_temperature_and_top_p_are_not_forwarded_gpt56_rejects_both(self):
        """Phase 4 regression guard: the model-roster upgrade to GPT-5.6
        Sol/Terra surfaced that temperature/top_p are rejected the same
        way `stop` already was — see the version-note in
        openai_adapter.py's translate_request."""
        payload = self.adapter.translate_request(
            _unified_request(temperature=0.4, top_p=0.9), provider_model="gpt-5.6-sol"
        )
        assert "temperature" not in payload
        assert "top_p" not in payload

    def test_model_field_is_the_provider_model_not_the_unified_id(self):
        payload = self.adapter.translate_request(_unified_request(), provider_model="gpt-5.4")
        assert payload["model"] == "gpt-5.4"

    def test_no_system_message_omitted_when_absent(self):
        payload = self.adapter.translate_request(
            _unified_request(system=None), provider_model="gpt-5.4"
        )
        assert payload["messages"][0]["role"] == "user"


class TestAnthropicTranslation:
    def setup_method(self):
        self.adapter = AnthropicAdapter(api_key="sk-ant-test")

    def test_system_prompt_is_top_level_field_not_a_message(self):
        payload = self.adapter.translate_request(
            _unified_request(), provider_model="claude-sonnet-5"
        )
        assert payload["system"] == "You are a helpful enterprise assistant."
        assert all(m["role"] != "system" for m in payload["messages"])

    def test_max_tokens_key_is_max_tokens_and_required(self):
        payload = self.adapter.translate_request(
            _unified_request(), provider_model="claude-sonnet-5"
        )
        assert payload["max_tokens"] == 256

    def test_stop_sequences_key_is_stop_sequences(self):
        payload = self.adapter.translate_request(
            _unified_request(), provider_model="claude-sonnet-5"
        )
        assert payload["stop_sequences"] == ["\n\nEND"]
        assert "stop" not in payload

    def test_no_system_key_when_absent(self):
        payload = self.adapter.translate_request(
            _unified_request(system=None), provider_model="claude-sonnet-5"
        )
        assert "system" not in payload


class TestOllamaTranslation:
    def setup_method(self):
        self.adapter = OllamaAdapter(base_url="http://localhost:11434")

    def test_system_prompt_becomes_first_message(self):
        payload = self.adapter.translate_request(_unified_request(), provider_model="llama3.2")
        assert payload["messages"][0] == {
            "role": "system",
            "content": "You are a helpful enterprise assistant.",
        }

    def test_max_tokens_maps_to_options_num_predict(self):
        payload = self.adapter.translate_request(_unified_request(), provider_model="llama3.2")
        assert payload["options"]["num_predict"] == 256
        assert "max_tokens" not in payload

    def test_stop_sequences_map_to_options_stop(self):
        payload = self.adapter.translate_request(_unified_request(), provider_model="llama3.2")
        assert payload["options"]["stop"] == ["\n\nEND"]

    def test_temperature_lives_under_options(self):
        payload = self.adapter.translate_request(_unified_request(), provider_model="llama3.2")
        assert payload["options"]["temperature"] == 0.4
