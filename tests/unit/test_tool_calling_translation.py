"""
test_tool_calling_translation.py

Phase 8 test plan (docs/PHASE8_KICKOFF_SCOPING.md §7): "A shared tool
definition translates correctly to all four providers' request shapes...
each provider's distinct tool-call response shape normalizes back to one
ToolCall list; the full round trip (request -> provider tool call ->
gateway normalizes -> client sends tool message -> gateway re-translates
into history) for at least one provider per adapter family."

Adapter-level, same convention as test_schema_normalization.py /
test_response_normalization.py — direct calls against each adapter's
translate_request/translate_response, no HTTP, no fakeredis, matching
this codebase's existing pattern for pure translation-logic tests.
"""

from __future__ import annotations

import json

import pytest

from app.core.schema import (
    ChatMessage,
    ForcedToolChoice,
    ToolCall,
    ToolDefinition,
    UnifiedChatRequest,
)
from app.providers.anthropic_adapter import AnthropicAdapter
from app.providers.gemini_adapter import GeminiAdapter
from app.providers.ollama_adapter import OllamaAdapter
from app.providers.openai_adapter import OpenAIAdapter

_SHARED_TOOL = ToolDefinition(
    name="get_weather",
    description="Get the current weather for a city.",
    parameters={
        "type": "object",
        "properties": {"city": {"type": "string"}},
        "required": ["city"],
    },
)


def _request(**overrides) -> UnifiedChatRequest:
    defaults = {
        "model": "placeholder:placeholder",
        "messages": [ChatMessage(role="user", content="weather in Pune?")],
        "tools": [_SHARED_TOOL],
    }
    defaults.update(overrides)
    return UnifiedChatRequest(**defaults)


# -- schema-level guard: tools + streaming is rejected up front -------------


def test_tools_plus_streaming_is_rejected_at_the_schema_boundary():
    with pytest.raises(Exception) as exc_info:
        UnifiedChatRequest(
            model="openai:gpt-5.4",
            messages=[ChatMessage(role="user", content="hi")],
            tools=[_SHARED_TOOL],
            stream=True,
        )
    assert "stream" in str(exc_info.value).lower()


def test_tools_without_streaming_is_fine():
    req = _request(model="openai:gpt-5.4", stream=False)
    assert req.tools == [_SHARED_TOOL]


# -- OpenAI -------------------------------------------------------------


class TestOpenAIToolCalling:
    def setup_method(self):
        self.adapter = OpenAIAdapter(api_key="sk-test")

    def test_tools_and_tool_choice_translate(self):
        req = _request(model="openai:gpt-5.6-sol", tool_choice="required")
        payload = self.adapter.translate_request(req, provider_model="gpt-5.6-sol")
        assert payload["tools"] == [
            {
                "type": "function",
                "name": "get_weather",
                "description": "Get the current weather for a city.",
                "parameters": _SHARED_TOOL.parameters,
                "strict": False,
            }
        ]
        assert payload["tool_choice"] == "required"

    def test_forced_tool_choice_translates_to_named_function_object(self):
        req = _request(model="openai:gpt-5.6-sol", tool_choice=ForcedToolChoice(name="get_weather"))
        payload = self.adapter.translate_request(req, provider_model="gpt-5.6-sol")
        assert payload["tool_choice"] == {"type": "function", "name": "get_weather"}

    def test_tool_call_response_extracts_to_unified_tool_call_and_sets_finish_reason(self):
        raw = {
            "id": "resp_1",
            "model": "gpt-5.6-sol",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_abc",
                    "name": "get_weather",
                    "arguments": '{"city": "Pune"}',
                }
            ],
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        unified = self.adapter.translate_response(
            raw, request=_request(model="openai:gpt-5.6-sol"), provider_model="gpt-5.6-sol"
        )
        assert unified.choices[0].finish_reason == "tool_calls"
        assert unified.choices[0].message.tool_calls == [
            ToolCall(id="call_abc", name="get_weather", arguments='{"city": "Pune"}')
        ]

    def test_history_round_trip_builds_function_call_and_output_items(self):
        call = ToolCall(id="call_abc", name="get_weather", arguments='{"city": "Pune"}')
        assistant_msg = ChatMessage(role="assistant", content="", tool_calls=[call])
        tool_msg = ChatMessage(role="tool", content="72F sunny", tool_call_id="call_abc")
        req = UnifiedChatRequest(
            model="openai:gpt-5.6-sol",
            messages=[ChatMessage(role="user", content="weather?"), assistant_msg, tool_msg],
        )
        payload = self.adapter.translate_request(req, provider_model="gpt-5.6-sol")
        assert payload["input"][1] == {
            "type": "function_call",
            "call_id": "call_abc",
            "name": "get_weather",
            "arguments": '{"city": "Pune"}',
        }
        assert payload["input"][2] == {
            "type": "function_call_output",
            "call_id": "call_abc",
            "output": "72F sunny",
        }

    def test_no_finish_reason_override_when_no_tool_calls_present(self):
        raw = {
            "id": "resp_2",
            "model": "gpt-5.6-sol",
            "output": [
                {
                    "type": "message",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "hi"}],
                }
            ],
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        unified = self.adapter.translate_response(
            raw, request=_request(model="openai:gpt-5.6-sol"), provider_model="gpt-5.6-sol"
        )
        assert unified.choices[0].finish_reason == "stop"
        assert unified.choices[0].message.tool_calls is None


# -- Anthropic ------------------------------------------------------------


class TestAnthropicToolCalling:
    def setup_method(self):
        self.adapter = AnthropicAdapter(api_key="sk-ant-test")

    def test_tools_translate_to_input_schema_shape(self):
        req = _request(model="anthropic:claude-sonnet-5")
        payload = self.adapter.translate_request(req, provider_model="claude-sonnet-5")
        assert payload["tools"] == [
            {
                "name": "get_weather",
                "description": "Get the current weather for a city.",
                "input_schema": _SHARED_TOOL.parameters,
                "strict": False,
            }
        ]

    @pytest.mark.parametrize(
        "unified_choice,expected",
        [
            ("auto", {"type": "auto"}),
            ("none", {"type": "none"}),
            ("required", {"type": "any"}),  # NOT a literal "required" string
        ],
    )
    def test_tool_choice_mode_translation(self, unified_choice, expected):
        req = _request(model="anthropic:claude-sonnet-5", tool_choice=unified_choice)
        payload = self.adapter.translate_request(req, provider_model="claude-sonnet-5")
        assert payload["tool_choice"] == expected

    def test_forced_tool_choice_translates_to_tool_type(self):
        req = _request(model="anthropic:claude-sonnet-5", tool_choice=ForcedToolChoice(name="get_weather"))
        payload = self.adapter.translate_request(req, provider_model="claude-sonnet-5")
        assert payload["tool_choice"] == {"type": "tool", "name": "get_weather"}

    def test_tool_use_response_extracts_to_unified_tool_call(self):
        raw = {
            "id": "msg_1",
            "model": "claude-sonnet-5",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Pune"}}
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 5, "output_tokens": 3},
        }
        unified = self.adapter.translate_response(
            raw, request=_request(model="anthropic:claude-sonnet-5"), provider_model="claude-sonnet-5"
        )
        assert unified.choices[0].finish_reason == "tool_calls"
        assert unified.choices[0].message.tool_calls == [
            ToolCall(id="toolu_1", name="get_weather", arguments=json.dumps({"city": "Pune"}))
        ]

    def test_history_round_trip_puts_tool_result_in_a_user_message(self):
        call = ToolCall(id="toolu_1", name="get_weather", arguments='{"city": "Pune"}')
        assistant_msg = ChatMessage(role="assistant", content="", tool_calls=[call])
        tool_msg = ChatMessage(role="tool", content="72F sunny", tool_call_id="toolu_1")
        req = UnifiedChatRequest(
            model="anthropic:claude-sonnet-5",
            messages=[ChatMessage(role="user", content="weather?"), assistant_msg, tool_msg],
        )
        payload = self.adapter.translate_request(req, provider_model="claude-sonnet-5")

        assert payload["messages"][1] == {
            "role": "assistant",
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"city": "Pune"}}
            ],
        }
        # The tool role does not exist for Anthropic -- the result lives
        # inside a USER message's content array.
        assert payload["messages"][2] == {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "72F sunny"}],
        }

    def test_consecutive_tool_messages_are_coalesced_into_one_user_message(self):
        """Parallel tool calls answered in one batch must become ONE user
        message with multiple tool_result blocks, not separate messages —
        a malformed conversation from Anthropic's point of view otherwise."""
        assistant_msg = ChatMessage(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="a", name="f1", arguments="{}"),
                ToolCall(id="b", name="f2", arguments="{}"),
            ],
        )
        req = UnifiedChatRequest(
            model="anthropic:claude-sonnet-5",
            messages=[
                ChatMessage(role="user", content="q"),
                assistant_msg,
                ChatMessage(role="tool", content="r1", tool_call_id="a"),
                ChatMessage(role="tool", content="r2", tool_call_id="b"),
            ],
        )
        payload = self.adapter.translate_request(req, provider_model="claude-sonnet-5")
        assert len(payload["messages"]) == 3  # user, assistant, ONE coalesced user
        assert payload["messages"][2]["role"] == "user"
        assert payload["messages"][2]["content"] == [
            {"type": "tool_result", "tool_use_id": "a", "content": "r1"},
            {"type": "tool_result", "tool_use_id": "b", "content": "r2"},
        ]


# -- Gemini ------------------------------------------------------------------


class TestGeminiToolCalling:
    def setup_method(self):
        self.adapter = GeminiAdapter(api_key="test-gemini-key")

    def test_tools_translate_to_function_declarations(self):
        req = _request(model="gemini:gemini-3.6-flash")
        payload = self.adapter.translate_request(req, provider_model="gemini-3.6-flash")
        decl = payload["tools"][0]["functionDeclarations"][0]
        assert decl["name"] == "get_weather"
        # Case-converted to Gemini's uppercase OpenAPI-schema dialect.
        assert decl["parameters"]["type"] == "OBJECT"
        assert decl["parameters"]["properties"]["city"]["type"] == "STRING"

    def test_tool_choice_modes_translate(self):
        for mode, expected in [("auto", "AUTO"), ("none", "NONE"), ("required", "ANY")]:
            req = _request(model="gemini:gemini-3.6-flash", tool_choice=mode)
            payload = self.adapter.translate_request(req, provider_model="gemini-3.6-flash")
            assert payload["toolConfig"]["functionCallingConfig"]["mode"] == expected

    def test_forced_tool_choice_sets_allowed_function_names(self):
        req = _request(model="gemini:gemini-3.6-flash", tool_choice=ForcedToolChoice(name="get_weather"))
        payload = self.adapter.translate_request(req, provider_model="gemini-3.6-flash")
        cfg = payload["toolConfig"]["functionCallingConfig"]
        assert cfg["mode"] == "ANY"
        assert cfg["allowedFunctionNames"] == ["get_weather"]

    def test_function_call_response_uses_real_id_when_gemini_provides_one(self):
        function_call = {"id": "real-id-1", "name": "get_weather", "args": {"city": "Pune"}}
        raw = {
            "candidates": [
                {
                    "content": {"parts": [{"functionCall": function_call}]},
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3},
        }
        unified = self.adapter.translate_response(
            raw, request=_request(model="gemini:gemini-3.6-flash"), provider_model="gemini-3.6-flash"
        )
        assert unified.choices[0].finish_reason == "tool_calls"
        call = unified.choices[0].message.tool_calls[0]
        assert call.id == "real-id-1"
        assert call.name == "get_weather"
        assert json.loads(call.arguments) == {"city": "Pune"}

    def test_function_call_response_synthesizes_an_id_when_gemini_omits_one(self):
        function_call = {"name": "get_weather", "args": {"city": "Pune"}}
        raw = {
            "candidates": [{"content": {"parts": [{"functionCall": function_call}]}, "finishReason": "STOP"}],
            "usageMetadata": {"promptTokenCount": 5, "candidatesTokenCount": 3},
        }
        unified = self.adapter.translate_response(
            raw, request=_request(model="gemini:gemini-3.6-flash"), provider_model="gemini-3.6-flash"
        )
        call = unified.choices[0].message.tool_calls[0]
        assert call.id.startswith("gwsyn_")

    def test_history_round_trip_drops_synthetic_id_but_keeps_a_real_one(self):
        synthetic_call = ToolCall(id="gwsyn_0", name="get_weather", arguments='{"city": "Pune"}')
        real_call = ToolCall(id="real-id-9", name="get_weather", arguments='{"city": "Pune"}')

        for call, should_have_id in [(synthetic_call, False), (real_call, True)]:
            assistant_msg = ChatMessage(role="assistant", content="", tool_calls=[call])
            tool_msg = ChatMessage(role="tool", content="72F sunny", tool_call_id=call.id)
            req = UnifiedChatRequest(
                model="gemini:gemini-3.6-flash",
                messages=[ChatMessage(role="user", content="weather?"), assistant_msg, tool_msg],
            )
            payload = self.adapter.translate_request(req, provider_model="gemini-3.6-flash")

            model_turn = payload["contents"][1]
            assert model_turn["role"] == "model"
            function_call_part = model_turn["parts"][0]["functionCall"]
            assert ("id" in function_call_part) is should_have_id

            response_turn = payload["contents"][2]
            assert response_turn["role"] == "user"
            function_response = response_turn["parts"][0]["functionResponse"]
            assert function_response["name"] == "get_weather"
            assert function_response["response"] == {"result": "72F sunny"}
            assert ("id" in function_response) is should_have_id


# -- Ollama ------------------------------------------------------------------


class TestOllamaToolCalling:
    def setup_method(self):
        self.adapter = OllamaAdapter(base_url="http://localhost:11434")

    def test_tools_translate_to_nested_function_shape(self):
        req = _request(model="ollama:llama3.2")
        payload = self.adapter.translate_request(req, provider_model="llama3.2")
        assert payload["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the current weather for a city.",
                    "parameters": _SHARED_TOOL.parameters,
                },
            }
        ]

    def test_tool_choice_is_accepted_but_has_no_wire_effect(self, caplog):
        import logging

        with caplog.at_level(logging.INFO, logger="gateway.ollama_adapter"):
            req = _request(model="ollama:llama3.2", tool_choice="required")
            payload = self.adapter.translate_request(req, provider_model="llama3.2")
        assert "tool_choice" not in payload
        assert any("no tool_choice equivalent" in r.getMessage() for r in caplog.records)

    def test_tool_calls_response_extracts_with_a_synthesized_id(self):
        raw = {
            "model": "llama3.2",
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"function": {"name": "get_weather", "arguments": {"city": "Pune"}}}],
            },
            "done": True,
            "prompt_eval_count": 5,
            "eval_count": 3,
        }
        unified = self.adapter.translate_response(
            raw, request=_request(model="ollama:llama3.2"), provider_model="llama3.2"
        )
        assert unified.choices[0].finish_reason == "tool_calls"
        call = unified.choices[0].message.tool_calls[0]
        assert call.id == "gwsyn_0"
        assert json.loads(call.arguments) == {"city": "Pune"}

    def test_tool_calls_response_accepts_string_arguments_too(self):
        """Some Ollama versions/models return arguments as a JSON string
        rather than a parsed object -- must not crash either way."""
        tool_call = {"function": {"name": "f", "arguments": '{"x": 1}'}}
        raw = {
            "model": "llama3.2",
            "message": {"role": "assistant", "content": "", "tool_calls": [tool_call]},
            "done": True,
        }
        unified = self.adapter.translate_response(
            raw, request=_request(model="ollama:llama3.2"), provider_model="llama3.2"
        )
        assert json.loads(unified.choices[0].message.tool_calls[0].arguments) == {"x": 1}

    def test_history_round_trip_uses_the_native_tool_role_verbatim(self):
        """Ollama needs ZERO structural translation for tool results — the
        unified `tool` role IS its wire shape."""
        call = ToolCall(id="gwsyn_0", name="get_weather", arguments='{"city": "Pune"}')
        assistant_msg = ChatMessage(role="assistant", content="", tool_calls=[call])
        tool_msg = ChatMessage(role="tool", content="72F sunny", tool_call_id="gwsyn_0")
        req = UnifiedChatRequest(
            model="ollama:llama3.2",
            messages=[ChatMessage(role="user", content="weather?"), assistant_msg, tool_msg],
        )
        payload = self.adapter.translate_request(req, provider_model="llama3.2")
        assert payload["messages"][1]["tool_calls"] == [
            {"function": {"name": "get_weather", "arguments": {"city": "Pune"}}}
        ]
        assert payload["messages"][2] == {"role": "tool", "content": "72F sunny", "tool_name": "get_weather"}


# -- cross-provider: the same tool definition, every adapter's shape --------


def _gemini_tool_name(payload: dict) -> str:
    return payload["tools"][0]["functionDeclarations"][0]["name"]


@pytest.mark.parametrize(
    "adapter,provider_model,extract_tool_name",
    [
        (OpenAIAdapter(api_key="k"), "gpt-5.6-sol", lambda p: p["tools"][0]["name"]),
        (AnthropicAdapter(api_key="k"), "claude-sonnet-5", lambda p: p["tools"][0]["name"]),
        (GeminiAdapter(api_key="k"), "gemini-3.6-flash", _gemini_tool_name),
        (OllamaAdapter(), "llama3.2", lambda p: p["tools"][0]["function"]["name"]),
    ],
    ids=["openai", "anthropic", "gemini", "ollama"],
)
def test_the_same_shared_tool_definition_translates_for_every_provider(
    adapter, provider_model, extract_tool_name
):
    req = _request(model=f"whatever:{provider_model}")
    payload = adapter.translate_request(req, provider_model=provider_model)
    assert extract_tool_name(payload) == "get_weather"
