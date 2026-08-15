from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import fakeredis
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

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
    reset before *and* after.

    Phase 3: HEALTH_CHECK_ENABLED is forced to "false" here even though
    the shipped .env.example default is "true" (per the TRD's own
    recommendation for production). The background health prober makes
    real HTTP calls to whatever providers are configured; every provider
    key in this test suite is a fake string ("test-openai-key" etc.), so
    letting it run would mean every test process spends real wall-clock
    time making doomed HTTP calls to api.openai.com et al. on a
    background task, which is slow, flaky, and not what any of these
    tests are trying to verify. Tests that specifically exercise the
    prober (test_health_checking.py) turn it on explicitly and swap in a
    FakeAdapter-backed registry.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://localhost:11434")
    monkeypatch.setenv("GATEWAY_ADMIN_KEY", ADMIN_KEY)
    monkeypatch.setenv("RATE_LIMIT_FAIL_OPEN", "true")
    monkeypatch.setenv("BATCH_QUEUE_MAX_WAIT_SECONDS", "1.5")
    monkeypatch.setenv("BATCH_QUEUE_POLL_INTERVAL_SECONDS", "0.05")
    monkeypatch.setenv("HEALTH_CHECK_ENABLED", "false")
    monkeypatch.setenv("HEALTH_CHECK_INTERVAL_SECONDS", "30")
    monkeypatch.setenv("CIRCUIT_BREAKER_FAILURE_THRESHOLD", "5")
    monkeypatch.setenv("CIRCUIT_BREAKER_WINDOW_SIZE", "10")
    # Production default is 60s (see .env.example); tests use a short
    # cooldown so an end-to-end Open -> Half-Open -> Closed test
    # (test_fallback_chain.py) can wait it out in real wall-clock time
    # without slowing the suite down. Tests that need to assert on the
    # *cooldown-not-yet-elapsed* window use a directly-constructed
    # CircuitBreaker with an injectable clock instead (test_circuit_breaker.py).
    monkeypatch.setenv("CIRCUIT_BREAKER_COOLDOWN_SECONDS", "0.2")
    monkeypatch.setenv("RETRY_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("RETRY_BASE_DELAY_SECONDS", "0.01")
    monkeypatch.setenv("RETRY_MAX_DELAY_SECONDS", "0.05")

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
    FakeServer, so tests never see each other's rate-limit/budget/circuit
    state). `decode_responses=True` matches production (see redis_client.py).
    """
    return fakeredis.FakeAsyncRedis(decode_responses=True)


@pytest.fixture
def app(fake_redis: fakeredis.FakeAsyncRedis) -> FastAPI:
    from app.main import create_app

    return create_app(redis_client=fake_redis)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    # Used as a context manager so FastAPI's lifespan (Redis wiring, team
    # seeding, the config-change pub/sub listener, Phase 3's health
    # checker task) actually runs — a bare `TestClient(app)` does not
    # trigger startup/shutdown events.
    with TestClient(app) as c:
        yield c


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    """
    Phase 4 test seam: an in-process OTel span exporter, so tests can
    assert on real emitted spans (names, kinds, attributes, status)
    without a live OTLP collector. Paired with the `traced_app`/
    `traced_client` fixtures below rather than the default `app`/`client`
    ones, since most tests don't care about tracing at all and
    `init_tracing()` still runs (cheaply) either way.
    """
    return InMemorySpanExporter()


@pytest.fixture
def traced_app(fake_redis: fakeredis.FakeAsyncRedis, span_exporter: InMemorySpanExporter) -> FastAPI:
    from app.main import create_app

    return create_app(redis_client=fake_redis, span_exporter_override=span_exporter)


@pytest.fixture
def traced_client(traced_app: FastAPI) -> TestClient:
    with TestClient(traced_app) as c:
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
    concurrency use this instead of the sync `client` fixture.
    """
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            yield ac


class FakeAdapter(ProviderAdapter):
    """
    Deterministic ProviderAdapter test double.

    Phase 3 additions:
    - `fail_times`: if > 0, `call()`/`stream()` raise a retryable
      ProviderError this many times before succeeding — lets retry/
      fallback tests script "fails twice, then works" without a real
      network flake.
    - `always_fail`: if True, every call raises (retryable unless
      `retryable=False` is passed) — used to trip circuit breakers and to
      exhaust whole fallback chains deterministically.
    - `latency_seconds`: injected `asyncio.sleep()` before responding, so
      health-check P99-latency tests don't need a real slow server.
    """

    provider_name = "fake"

    def __init__(
        self,
        *,
        response_text: str = "hello from fake adapter",
        stream_chunks: list[str] | None = None,
        usage_override: Usage | None = None,
        fail_times: int = 0,
        always_fail: bool = False,
        retryable: bool = True,
        error_type: str = "provider_error",
        status_code: int | None = 500,
        latency_seconds: float = 0.0,
    ) -> None:
        self.response_text = response_text
        self.stream_chunks = stream_chunks or ["Hel", "lo", "!"]
        self.usage_override = usage_override
        self.last_translated_request: UnifiedChatRequest | None = None
        self.call_count = 0
        self.yielded_count = 0
        self.stream_closed = False
        self.fail_times = fail_times
        self.always_fail = always_fail
        self.retryable = retryable
        self.error_type = error_type
        self.status_code = status_code
        self.latency_seconds = latency_seconds

    def translate_request(self, request: UnifiedChatRequest, *, provider_model: str) -> dict:
        self.last_translated_request = request
        return {"model": provider_model, "messages": [m.content for m in request.messages]}

    def _maybe_raise(self) -> None:
        from app.providers.base import ProviderError

        if self.always_fail or self.call_count <= self.fail_times:
            raise ProviderError(
                f"fake adapter scripted failure ({self.call_count}/{self.fail_times})",
                status_code=self.status_code,
                retryable=self.retryable,
                error_type=self.error_type,
            )

    async def call(self, payload: dict) -> dict:
        import asyncio

        self.call_count += 1
        if self.latency_seconds:
            await asyncio.sleep(self.latency_seconds)
        self._maybe_raise()
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
        self.call_count += 1
        try:
            self._maybe_raise()
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
