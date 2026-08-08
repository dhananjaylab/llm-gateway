from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.core import config as config_module
from app.core.schema import (
    ChatMessage,
    UnifiedChatRequest,
    UnifiedChatResponse,
    UnifiedChoice,
    UnifiedStreamChunk,
    Usage,
)
from app.providers import registry as registry_module
from app.providers.base import ProviderAdapter

# Raw demo keys matching config/teams.yaml's committed hashes.
DATA_SCIENCE_KEY = "sk-gw-datascience-demo-001"
PRODUCT_ENG_KEY = "sk-gw-producteng-demo-002"
BATCH_DEVS_KEY = "sk-gw-batchdevs-demo-003"

# Test-only admin secret — set into the environment by the autouse fixture
# below so app.core.config.get_gateway_settings() picks it up.
ADMIN_KEY = "test-admin-key-do-not-use-in-prod"


@pytest.fixture(autouse=True)
def _reset_singleton_caches(monkeypatch):
    """
    Every cached singleton (provider settings, gateway settings, adapter
    registry) is process-lifetime by design (see app/core/config.py's
    lru_cache usage). Tests must not leak state between each other, so
    reset before *and* after. Team config itself no longer lives in an
    in-memory singleton as of Phase 2 — it lives in the per-test
    `fake_redis` instance, which is naturally isolated per test already.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("GATEWAY_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("RATE_LIMIT_FAIL_OPEN", "true")
    monkeypatch.setenv("BATCH_QUEUE_MAX_WAIT_SECONDS", "1.5")
    monkeypatch.setenv("BATCH_QUEUE_POLL_INTERVAL_SECONDS", "0.05")

    config_module.reset_config_cache()
    config_module.reset_provider_settings_cache()
    config_module.reset_gateway_settings_cache()
    registry_module.reset_registry_cache()
    yield
    config_module.reset_config_cache()
    config_module.reset_provider_settings_cache()
    config_module.reset_gateway_settings_cache()
    registry_module.reset_registry_cache()


@pytest.fixture
def fake_redis() -> fakeredis.FakeAsyncRedis:
    """
    A fresh, isolated in-memory Redis double per test (no shared
    FakeServer, so tests never see each other's rate-limit/budget state).
    `decode_responses=True` matches production (see redis_client.py).
    """
    return fakeredis.FakeAsyncRedis(decode_responses=True)


@pytest.fixture
def app(fake_redis: fakeredis.FakeAsyncRedis) -> FastAPI:
    from app.main import create_app

    return create_app(redis_client=fake_redis)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    # Used as a context manager so FastAPI's lifespan (Redis wiring, team
    # seeding, the config-change pub/sub listener) actually runs — a bare
    # `TestClient(app)` does not trigger startup/shutdown events.
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin_headers() -> dict:
    return {"X-Gateway-Admin-Key": ADMIN_KEY}


@asynccontextmanager
async def running_app_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """
    True-concurrency test helper: runs the app's lifespan and hands back
    an httpx.AsyncClient wired directly to the ASGI app (no network, no
    thread pool). Tests that need real `asyncio.gather()`-level
    concurrency (test_token_bucket_concurrency.py, test_priority_queue.py)
    use this instead of the sync `client` fixture, since TestClient's
    background-thread portal doesn't guarantee true interleaving the way
    a single event loop's `asyncio.gather` does.
    """
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


class FakeAdapter(ProviderAdapter):
    """
    Deterministic ProviderAdapter test double.

    `usage_override`, new in Phase 2: if given, BOTH the non-streaming
    `translate_response()` and the streaming terminal chunk report exactly
    this Usage — for budget/reconciliation tests that need to control the
    real cost/token count precisely. If omitted, behavior matches Phase 1
    exactly: `translate_response()` reports a fixed Usage(10, 5), and the
    streaming terminal chunk reports Usage(10, len(stream_chunks)) (i.e.
    output_tokens tracks how many chunks were actually sent) — collapsing
    these two into one shared default broke
    test_stream_chunks_arrive_in_order_and_unmodified's output_tokens
    assertion during this phase's own build, which is exactly the kind of
    default-divergence this docstring exists to prevent recurring.
    """

    provider_name = "fake"

    def __init__(
        self,
        *,
        response_text: str = "hello from fake adapter",
        stream_chunks: list[str] | None = None,
        usage_override: Usage | None = None,
    ) -> None:
        self.response_text = response_text
        self.stream_chunks = stream_chunks or ["Hel", "lo", "!"]
        self.usage_override = usage_override
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
        usage = self.usage_override or Usage(input_tokens=10, output_tokens=5)
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
            usage=usage,
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
            usage = self.usage_override or Usage(
                input_tokens=10, output_tokens=len(self.stream_chunks)
            )
            yield UnifiedStreamChunk(
                id="fake-stream-1",
                provider=self.provider_name,
                model_served=provider_model,
                delta="",
                finish_reason="stop",
                usage=usage,
            )
        finally:
            self.stream_closed = True


@pytest.fixture
def fake_adapter() -> FakeAdapter:
    return FakeAdapter()
