from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from app.core import config as config_module
from app.core.schema import (
    ChatMessage,
    UnifiedChatRequest,
    UnifiedChatResponse,
    UnifiedChoice,
    UnifiedStreamChunk,
    Usage,
)
from app.providers.base import ProviderAdapter
from app.providers import registry as registry_module

# Raw demo keys matching config/teams.yaml's committed hashes.
DATA_SCIENCE_KEY = "sk-gw-datascience-demo-001"
PRODUCT_ENG_KEY = "sk-gw-producteng-demo-002"
BATCH_DEVS_KEY = "sk-gw-batchdevs-demo-003"


@pytest.fixture(autouse=True)
def _reset_singleton_caches(monkeypatch):
    """
    Every cached singleton (config, provider settings, adapter registry) is
    process-lifetime by design in Phase 1 (no hot-reload yet). Tests must
    not leak state between each other, so reset before *and* after.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")

    config_module.reset_config_cache()
    config_module.reset_provider_settings_cache()
    registry_module.reset_registry_cache()
    yield
    config_module.reset_config_cache()
    config_module.reset_provider_settings_cache()
    registry_module.reset_registry_cache()


@pytest.fixture
def client() -> TestClient:
    from app.main import create_app

    return TestClient(create_app())


class FakeAdapter(ProviderAdapter):
    """
    Deterministic ProviderAdapter test double. Records the last payload it
    was asked to translate/call so tests can assert on what the route
    handler sent it, without any real network I/O.
    """

    provider_name = "fake"

    def __init__(
        self,
        *,
        response_text: str = "hello from fake adapter",
        stream_chunks: list[str] | None = None,
    ) -> None:
        self.response_text = response_text
        self.stream_chunks = stream_chunks or ["Hel", "lo", "!"]
        self.last_translated_request: UnifiedChatRequest | None = None
        self.call_count = 0
        self.yielded_count = 0
        self.stream_closed = False

    def translate_request(self, request: UnifiedChatRequest, *, provider_model: str) -> dict:
        self.last_translated_request = request
        return {"model": provider_model, "messages": [m.content for m in request.messages]}

    async def call(self, payload: dict) -> dict:
        self.call_count += 1
        return {"_fake_payload": payload}

    def translate_response(
        self, raw: dict, *, request: UnifiedChatRequest, provider_model: str
    ) -> UnifiedChatResponse:
        return UnifiedChatResponse(
            id="fake-id-1",
            provider=self.provider_name,
            model_requested=request.model,
            model_served=provider_model,
            choices=[
                UnifiedChoice(
                    index=0,
                    message=ChatMessage(role="assistant", content=self.response_text),
                    finish_reason="stop",
                )
            ],
            usage=Usage(input_tokens=10, output_tokens=5),
        )

    async def stream(
        self, payload: dict, *, request: UnifiedChatRequest, provider_model: str
    ) -> AsyncIterator[UnifiedStreamChunk]:
        try:
            for text in self.stream_chunks:
                self.yielded_count += 1
                yield UnifiedStreamChunk(
                    id="fake-stream-1",
                    provider=self.provider_name,
                    model_served=provider_model,
                    delta=text,
                )
            yield UnifiedStreamChunk(
                id="fake-stream-1",
                provider=self.provider_name,
                model_served=provider_model,
                delta="",
                finish_reason="stop",
                usage=Usage(input_tokens=10, output_tokens=len(self.stream_chunks)),
            )
        finally:
            self.stream_closed = True


@pytest.fixture
def fake_adapter() -> FakeAdapter:
    return FakeAdapter()
