"""
test_v1_chat_cache_wiring.py

Direct-call harness for app.api.v1_chat.chat_completions -- validates the
Phase 9b wiring itself (docs/PHASE9B_KICKOFF_SCOPING.md Sec 2/9) without
needing the full FastAPI app/TestClient stack: a cache HIT must never
touch rate_limiter/budget_enforcer/fallback_router at all (spied), and a
cache MISS must fall through to the (stubbed) existing pipeline and then
call cache_gateway.write_through() with the real completed response.

This is a supplementary, narrowly-scoped test -- the full existing
integration-style suite (test_auth.py, test_fallback_chain.py,
test_budget_enforcement.py, etc.) still needs to run against the real,
complete app to confirm zero regressions end-to-end; seeing those pass
unchanged is this delivery's own stated residual verification step (see
the implementation guide).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi import Response

from app.api.v1_chat import chat_completions
from app.caching.cache_gateway import CacheGateway
from app.caching.exact_cache import ExactMatchCache
from app.caching.semantic_cache import SemanticCache
from app.core.config import GatewaySettings, TeamConfig
from app.core.schema import ChatMessage, UnifiedChatRequest, UnifiedChatResponse, UnifiedChoice, Usage


class _FixedVectorEmbedder:
    dimension = 4

    async def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]


class _NeverCallMe:
    """Any attribute access is fine; any *call* fails the test -- stands
    in for rate_limiter/budget_enforcer/combined_quota_checker/
    fallback_router/org_store on the cache-HIT path, which must never
    touch any of them."""

    def __getattr__(self, name):
        async def _boom(*args, **kwargs):
            raise AssertionError(f"{name} must never be called on a cache hit")

        return _boom


@dataclass
class _RLDecision:
    allowed: bool = True
    remaining_rpm: int = 99
    remaining_tpm: int = 9999
    limit_type: str | None = None
    retry_after_seconds: int | None = None


@dataclass
class _BudgetDecision:
    allowed: bool = True
    spend_usd: float = 0.0
    cap_usd: float = 100.0
    period: str = "2026-09"
    warning_fraction: float | None = None


@dataclass
class _CombinedDecision:
    budget_denied: bool = False
    budget: _BudgetDecision = field(default_factory=_BudgetDecision)
    rate_limit: _RLDecision = field(default_factory=_RLDecision)


class _StubOrgStore:
    async def get_org(self, org_id):
        return None  # exercises the "no matching OrgConfig" skip path cleanly


class _StubCombinedQuotaChecker:
    async def check(self, *, team, estimated_tokens):
        return _CombinedDecision()


class _StubRateLimiter:
    def __init__(self):
        self.reconcile_calls = []

    async def reconcile(self, *, team, reserved_tokens, actual_tokens):
        self.reconcile_calls.append((reserved_tokens, actual_tokens))

    async def reconcile_org(self, **kwargs):
        pass


class _StubBudgetEnforcer:
    def __init__(self):
        self.record_spend_calls = []

    async def record_spend(self, team, cost_usd):
        self.record_spend_calls.append(cost_usd)
        return _BudgetDecision(spend_usd=cost_usd)


class _StubAdapter:
    provider_name = "openai"


class _StubFallbackRouter:
    def __init__(self, response: UnifiedChatResponse):
        self._response = response
        self.execute_calls = 0

    def resolve_chain(self, model_id):
        return [model_id]

    async def execute_non_streaming(self, **kwargs):
        self.execute_calls += 1
        return _StubAdapter(), self._response.model_served, self._response


def _team(**overrides) -> TeamConfig:
    defaults = dict(
        team_id="data-science",
        api_key_hash="x",
        allowed_models=["openai:gpt-5.4"],
        semantic_cache_enabled=True,
    )
    defaults.update(overrides)
    return TeamConfig(**defaults)


def _req(**overrides) -> UnifiedChatRequest:
    defaults = dict(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="what is a token bucket?")])
    defaults.update(overrides)
    return UnifiedChatRequest(**defaults)


def _cache_gateway() -> CacheGateway:
    r = fakeredis.FakeAsyncRedis(decode_responses=True, version=(8, 0, 0))
    exact = ExactMatchCache(r, ttl_seconds=3600)
    semantic = SemanticCache(r, ttl_seconds=3600, similarity_threshold=0.97, max_entries_per_scope=5000)
    settings = GatewaySettings(cache_enabled=True)
    return CacheGateway(exact_cache=exact, semantic_cache=semantic, embedding_provider=_FixedVectorEmbedder(), settings=settings)


def _fake_http_request(app_state):
    return SimpleNamespace(app=SimpleNamespace(state=app_state))


def _app_state(*, cache_gateway, never_call_pipeline: bool, stub_fallback_router=None):
    pipeline = _NeverCallMe() if never_call_pipeline else None
    return SimpleNamespace(
        tracer=None,
        metrics=None,
        rate_limiter=pipeline or _StubRateLimiter(),
        budget_enforcer=pipeline or _StubBudgetEnforcer(),
        batch_queue=pipeline,
        combined_quota_checker=pipeline or _StubCombinedQuotaChecker(),
        pricing={},
        fallback_router=stub_fallback_router or pipeline,
        org_store=pipeline or _StubOrgStore(),
        cache_gateway=cache_gateway,
    )


async def test_a_cache_hit_never_touches_rate_limiter_budget_or_fallback_router():
    cache_gateway = _cache_gateway()
    team = _team()
    request = _req()

    completed = UnifiedChatResponse(
        id="r1",
        provider="openai",
        model_requested="openai:gpt-5.4",
        model_served="gpt-5.4-served",
        choices=[UnifiedChoice(index=0, message=ChatMessage(role="assistant", content="42"), finish_reason="stop")],
        usage=Usage(input_tokens=5, output_tokens=5),
    )
    # Pre-populate the cache directly (bypassing the route) so the SECOND
    # call below is a guaranteed exact hit.
    await cache_gateway.write_through(request=request, team=team, response=completed, provider="openai", cost_usd=0.002)

    app_state = _app_state(cache_gateway=cache_gateway, never_call_pipeline=True)
    http_request = _fake_http_request(app_state)
    response = Response()

    result = await chat_completions(request=request, http_request=http_request, response=response, team=team)

    assert result.choices[0].message.content == "42"
    assert result.provider == "cache:openai"
    assert response.headers["X-Gateway-Cache"] == "hit"
    assert response.headers["X-Gateway-Served-Model"] == "cache:openai:gpt-5.4-served"
    # No rate-limit headers -- nothing was checked.
    assert "X-RateLimit-Remaining-RPM" not in response.headers


async def test_a_cache_miss_falls_through_to_the_pipeline_and_writes_through_on_success():
    cache_gateway = _cache_gateway()
    team = _team()
    request = _req()

    completed = UnifiedChatResponse(
        id="r2",
        provider="openai",
        model_requested="openai:gpt-5.4",
        model_served="gpt-5.4-served",
        choices=[UnifiedChoice(index=0, message=ChatMessage(role="assistant", content="fresh answer"), finish_reason="stop")],
        usage=Usage(input_tokens=5, output_tokens=5),
    )
    stub_router = _StubFallbackRouter(completed)
    app_state = _app_state(cache_gateway=cache_gateway, never_call_pipeline=False, stub_fallback_router=stub_router)
    http_request = _fake_http_request(app_state)
    response = Response()

    result = await chat_completions(request=request, http_request=http_request, response=response, team=team)

    assert result.choices[0].message.content == "fresh answer"
    assert stub_router.execute_calls == 1
    assert response.headers["X-RateLimit-Remaining-RPM"] == "99"
    assert "X-Gateway-Cache" not in response.headers

    # And it must now be cached: a second call is a hit, never touching
    # the fallback router again.
    app_state_2 = _app_state(cache_gateway=cache_gateway, never_call_pipeline=True)
    http_request_2 = _fake_http_request(app_state_2)
    response_2 = Response()
    second = await chat_completions(request=request, http_request=http_request_2, response=response_2, team=team)
    assert second.choices[0].message.content == "fresh answer"
    assert response_2.headers["X-Gateway-Cache"] == "hit"


async def test_a_request_with_semantic_caching_off_for_the_team_never_enters_the_cache_span_meaningfully():
    """double opt-in (Sec 4.3): team.semantic_cache_enabled=False must
    still fall straight through to the normal pipeline, same as a
    genuine miss -- and must not accidentally write anything either."""
    cache_gateway = _cache_gateway()
    team = _team(semantic_cache_enabled=False)
    request = _req()

    completed = UnifiedChatResponse(
        id="r3",
        provider="openai",
        model_requested="openai:gpt-5.4",
        model_served="gpt-5.4-served",
        choices=[UnifiedChoice(index=0, message=ChatMessage(role="assistant", content="answer"), finish_reason="stop")],
        usage=Usage(input_tokens=5, output_tokens=5),
    )
    stub_router = _StubFallbackRouter(completed)
    app_state = _app_state(cache_gateway=cache_gateway, never_call_pipeline=False, stub_fallback_router=stub_router)
    http_request = _fake_http_request(app_state)
    response = Response()

    result = await chat_completions(request=request, http_request=http_request, response=response, team=team)
    assert result.choices[0].message.content == "answer"
    assert stub_router.execute_calls == 1

    from app.caching.fingerprint import scope

    s = scope(team=team, requested_model=request.model)
    assert await cache_gateway._exact.get(scope=s, request=request) is None, (
        "an ineligible (opted-out) team's completion must never be written to the cache either"
    )
