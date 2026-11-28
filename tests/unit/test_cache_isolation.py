"""
test_cache_isolation.py

Verifies docs/PHASE9B_KICKOFF_SCOPING.md Sec 4.1/9's structural-isolation
claim directly, through CacheGateway, not just at the fingerprint-module
level (test_cache_fingerprint.py already covers scope() in isolation) --
this is the test that stands in for "two teams never see each other's
cache entries" as an end-to-end behavioral guarantee, not just an
assumption about how scope() happens to hash.
"""

from __future__ import annotations

import fakeredis

from app.caching.cache_gateway import CacheGateway
from app.caching.exact_cache import ExactMatchCache
from app.caching.semantic_cache import SemanticCache
from app.core.config import GatewaySettings, TeamConfig
from app.core.schema import ChatMessage, UnifiedChatResponse, UnifiedChatRequest, UnifiedChoice, Usage


class _FixedVectorEmbedder:
    """Every text embeds to the identical vector -- isolates the isolation
    test from embedding-quality concerns entirely; if two teams' entries
    ever collide here, it can only be because scope isolation failed, not
    because two different texts coincidentally embedded close together."""

    dimension = 4

    async def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]


def _settings() -> GatewaySettings:
    return GatewaySettings(cache_enabled=True)


def _team(team_id: str, **overrides) -> TeamConfig:
    defaults = dict(team_id=team_id, api_key_hash="x", allowed_models=["openai:gpt-5.4"], semantic_cache_enabled=True)
    defaults.update(overrides)
    return TeamConfig(**defaults)


def _gateway(redis):
    exact = ExactMatchCache(redis, ttl_seconds=3600)
    semantic = SemanticCache(redis, ttl_seconds=3600, similarity_threshold=0.97, max_entries_per_scope=5000)
    return CacheGateway(exact_cache=exact, semantic_cache=semantic, embedding_provider=_FixedVectorEmbedder(), settings=_settings())


def _req() -> UnifiedChatRequest:
    return UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="what is a token bucket?")])


def _response(text: str) -> UnifiedChatResponse:
    return UnifiedChatResponse(
        id="r1",
        provider="fake",
        model_requested="openai:gpt-5.4",
        model_served="gpt-5.4-served",
        choices=[UnifiedChoice(index=0, message=ChatMessage(role="assistant", content=text), finish_reason="stop")],
        usage=Usage(input_tokens=5, output_tokens=5),
    )


async def test_two_different_teams_never_see_each_others_cached_answer():
    r = fakeredis.FakeAsyncRedis(decode_responses=True, version=(8, 0, 0))
    gw = _gateway(r)
    team_a, team_b = _team("team-a"), _team("team-b")

    await gw.write_through(request=_req(), team=team_a, response=_response("answer-for-a"), provider="fake", cost_usd=0.001)

    hit_a = await gw.lookup(request=_req(), team=team_a)
    hit_b = await gw.lookup(request=_req(), team=team_b)

    assert hit_a.layer == "exact"
    assert hit_a.response.choices[0].message.content == "answer-for-a"
    assert hit_b.layer == "miss", "team-b must never see team-a's cached entry"


async def test_the_same_team_under_two_different_policies_never_cross():
    """A pii_redaction toggle must produce a different scope -- the same
    team, same model, same prompt, but a policy change must not silently
    serve an answer generated under the old policy."""
    r = fakeredis.FakeAsyncRedis(decode_responses=True, version=(8, 0, 0))
    gw = _gateway(r)

    from app.core.config import TeamPolicy

    team_before = _team("team-c", policy=TeamPolicy(pii_redaction=False))
    team_after = _team("team-c", policy=TeamPolicy(pii_redaction=True))

    await gw.write_through(
        request=_req(), team=team_before, response=_response("answer-under-old-policy"), provider="fake", cost_usd=0.001
    )

    hit_after_policy_change = await gw.lookup(request=_req(), team=team_after)
    assert hit_after_policy_change.layer == "miss", (
        "a policy change must invalidate the scope, not silently serve the old-policy answer"
    )


async def test_the_same_team_under_two_different_requested_models_never_cross():
    r = fakeredis.FakeAsyncRedis(decode_responses=True, version=(8, 0, 0))
    gw = _gateway(r)
    team = _team("team-d", allowed_models=["openai:gpt-5.4", "anthropic:claude-sonnet-5"])

    await gw.write_through(request=_req(), team=team, response=_response("gpt-answer"), provider="fake", cost_usd=0.001)

    other_model_req = UnifiedChatRequest(
        model="anthropic:claude-sonnet-5", messages=[ChatMessage(role="user", content="what is a token bucket?")]
    )
    hit = await gw.lookup(request=other_model_req, team=team)
    assert hit.layer == "miss"
