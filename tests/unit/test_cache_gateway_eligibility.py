"""
test_cache_gateway_eligibility.py

Verifies docs/PHASE9B_KICKOFF_SCOPING.md Sec 4.3/4.4/4.5: streaming,
tool-calling, high-temperature, and double-opt-in-not-satisfied requests
are all `ineligible` and -- the part that actually matters -- never touch
Redis or the embedder to reach that conclusion. Every combination gets its
own case, not one combined assertion.
"""

from __future__ import annotations

import fakeredis
import pytest

from app.caching.cache_gateway import CacheGateway
from app.caching.exact_cache import ExactMatchCache
from app.caching.semantic_cache import SemanticCache
from app.core.config import GatewaySettings, TeamConfig
from app.core.schema import ChatMessage, UnifiedChatRequest


class _SpyEmbeddingProvider:
    """Records whether embed() was ever called -- ineligible requests must
    never reach it (docs/PHASE9B_KICKOFF_SCOPING.md Sec 3.1's whole point:
    the eligibility gate runs before anything expensive)."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.dimension = 4

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return [1.0, 0.0, 0.0, 0.0]


def _settings(**overrides) -> GatewaySettings:
    defaults = dict(cache_enabled=True, cache_max_temperature=0.3)
    defaults.update(overrides)
    return GatewaySettings(**defaults)


def _team(**overrides) -> TeamConfig:
    defaults = dict(
        team_id="data-science", api_key_hash="x", allowed_models=["openai:gpt-5.4"], semantic_cache_enabled=True
    )
    defaults.update(overrides)
    return TeamConfig(**defaults)


def _gateway(settings, embedder):
    r = fakeredis.FakeAsyncRedis(decode_responses=True, version=(8, 0, 0))
    exact = ExactMatchCache(r, ttl_seconds=3600)
    semantic = SemanticCache(r, ttl_seconds=3600, similarity_threshold=0.97, max_entries_per_scope=5000)
    return CacheGateway(exact_cache=exact, semantic_cache=semantic, embedding_provider=embedder, settings=settings)


def _req(**overrides) -> UnifiedChatRequest:
    defaults = dict(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="hi")])
    defaults.update(overrides)
    return UnifiedChatRequest(**defaults)


async def test_gateway_disabled_globally_is_ineligible_and_never_embeds():
    embedder = _SpyEmbeddingProvider()
    gw = _gateway(_settings(cache_enabled=False), embedder)
    result = await gw.lookup(request=_req(), team=_team())
    assert result.layer == "ineligible"
    assert embedder.calls == []


async def test_team_not_opted_in_is_ineligible_even_when_gateway_is_enabled():
    embedder = _SpyEmbeddingProvider()
    gw = _gateway(_settings(cache_enabled=True), embedder)
    result = await gw.lookup(request=_req(), team=_team(semantic_cache_enabled=False))
    assert result.layer == "ineligible"
    assert embedder.calls == []


async def test_streaming_request_is_ineligible_and_never_embeds():
    embedder = _SpyEmbeddingProvider()
    gw = _gateway(_settings(), embedder)
    result = await gw.lookup(request=_req(stream=True), team=_team())
    assert result.layer == "ineligible"
    assert embedder.calls == []


async def test_tool_calling_request_is_ineligible_and_never_embeds():
    from app.core.schema import ToolDefinition

    embedder = _SpyEmbeddingProvider()
    gw = _gateway(_settings(), embedder)
    tool = ToolDefinition(name="get_weather", parameters={"type": "object"})
    result = await gw.lookup(request=_req(tools=[tool]), team=_team())
    assert result.layer == "ineligible"
    assert embedder.calls == []


async def test_high_temperature_request_is_ineligible_and_never_embeds():
    embedder = _SpyEmbeddingProvider()
    gw = _gateway(_settings(cache_max_temperature=0.3), embedder)
    result = await gw.lookup(request=_req(temperature=0.8), team=_team())
    assert result.layer == "ineligible"
    assert embedder.calls == []


async def test_temperature_at_exactly_the_threshold_is_eligible():
    embedder = _SpyEmbeddingProvider()
    gw = _gateway(_settings(cache_max_temperature=0.3), embedder)
    result = await gw.lookup(request=_req(temperature=0.3), team=_team())
    assert result.layer != "ineligible"


async def test_unset_temperature_is_eligible():
    embedder = _SpyEmbeddingProvider()
    gw = _gateway(_settings(cache_max_temperature=0.3), embedder)
    result = await gw.lookup(request=_req(temperature=None), team=_team())
    assert result.layer != "ineligible"


async def test_an_eligible_request_with_an_empty_cache_reaches_the_embedder_exactly_once():
    """The other side of the same coin: an eligible request DOES call
    embed() -- confirms the spy would actually catch a regression, not
    just that ineligible paths skip it."""
    embedder = _SpyEmbeddingProvider()
    gw = _gateway(_settings(), embedder)
    result = await gw.lookup(request=_req(), team=_team())
    assert result.layer == "miss"
    assert len(embedder.calls) == 1
