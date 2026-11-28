"""
test_cache_fail_open.py

A cache is an optimization, never a new failure mode: every error below
the eligibility gate must degrade to a plain miss (lookup) or a logged
no-op (write_through), never an exception that reaches the caller and 500s
a request whose provider call would have succeeded. Found by reviewing
the v1_chat.py call sites while wiring Phase 9b, not in the kickoff doc.
"""

from __future__ import annotations

import fakeredis

from app.caching.cache_gateway import CacheGateway
from app.caching.exact_cache import ExactMatchCache
from app.caching.semantic_cache import SemanticCache
from app.core.config import GatewaySettings, TeamConfig
from app.core.schema import ChatMessage, UnifiedChatRequest, UnifiedChatResponse, UnifiedChoice, Usage


class _ExplodingEmbedder:
    dimension = 4

    async def embed(self, text: str) -> list[float]:
        raise RuntimeError("simulated: model download failed (huggingface.co unreachable)")


class _OkEmbedder:
    dimension = 4

    async def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]


def _gw(embedder):
    r = fakeredis.FakeAsyncRedis(decode_responses=True, version=(8, 0, 0))
    return CacheGateway(
        exact_cache=ExactMatchCache(r, ttl_seconds=3600),
        semantic_cache=SemanticCache(r, ttl_seconds=3600, similarity_threshold=0.97, max_entries_per_scope=5000),
        embedding_provider=embedder,
        settings=GatewaySettings(cache_enabled=True),
    ), r


def _team():
    return TeamConfig(team_id="t", api_key_hash="x", allowed_models=["openai:gpt-5.4"], semantic_cache_enabled=True)


def _req():
    return UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="hi")])


def _resp():
    return UnifiedChatResponse(
        id="r", provider="fake", model_requested="openai:gpt-5.4", model_served="m",
        choices=[UnifiedChoice(index=0, message=ChatMessage(role="assistant", content="a"), finish_reason="stop")],
        usage=Usage(input_tokens=1, output_tokens=1),
    )


async def test_lookup_degrades_to_a_miss_when_the_embedder_raises():
    gw, _ = _gw(_ExplodingEmbedder())
    result = await gw.lookup(request=_req(), team=_team())
    assert result.layer == "miss"


async def test_lookup_degrades_to_a_miss_when_redis_raises(monkeypatch):
    gw, _ = _gw(_OkEmbedder())

    async def _boom(*a, **k):
        raise ConnectionError("simulated redis outage")

    monkeypatch.setattr(gw._exact, "get", _boom)
    result = await gw.lookup(request=_req(), team=_team())
    assert result.layer == "miss"


async def test_write_through_never_raises_when_the_embedder_fails():
    gw, _ = _gw(_ExplodingEmbedder())
    await gw.write_through(request=_req(), team=_team(), response=_resp(), provider="fake", cost_usd=0.001)  # must not raise


async def test_write_through_never_raises_when_redis_fails(monkeypatch):
    gw, _ = _gw(_OkEmbedder())

    async def _boom(*a, **k):
        raise ConnectionError("simulated redis outage")

    monkeypatch.setattr(gw._exact, "set", _boom)
    await gw.write_through(request=_req(), team=_team(), response=_resp(), provider="fake", cost_usd=0.001)  # must not raise


async def test_ineligible_requests_never_reach_the_failing_component_at_all():
    """The eligibility gate still runs first and outside the try -- an
    ineligible request must not even attempt the embedder."""
    gw, _ = _gw(_ExplodingEmbedder())
    streaming = UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="hi")], stream=True)
    assert (await gw.lookup(request=streaming, team=_team())).layer == "ineligible"
