from __future__ import annotations

from app.core import config as config_module
from app.core.schema import ChatMessage, UnifiedChatRequest
from app.providers.gemini_adapter import GeminiAdapter
from app.providers import registry as registry_module


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


def test_provider_settings_load_from_dotenv(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    config_module.reset_provider_settings_cache()

    settings = config_module.get_provider_settings()

    assert settings.gemini_api_key is not None
