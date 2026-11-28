"""
test_cache_end_to_end.py

Verifies the full CacheGateway lookup/write_through cycle
(docs/PHASE9B_KICKOFF_SCOPING.md Sec 3.3's sequence diagram, end to end): a
miss populates both layers; a subsequent byte-identical request hits the
exact layer; a subsequent paraphrase (simulated via a fixed "close" vector,
since this file doesn't depend on network-reachable real embeddings --
test_embedding_provider.py covers the real FastEmbed integration
separately) hits the semantic layer; a parameter mismatch on an otherwise-
similar request is still a miss (Sec 4.5); a `finish_reason != "stop"`
response is never written to either layer (Sec 9 point 4).
"""

from __future__ import annotations

import fakeredis

from app.caching.cache_gateway import CacheGateway
from app.caching.exact_cache import ExactMatchCache
from app.caching.semantic_cache import SemanticCache
from app.core.config import GatewaySettings, TeamConfig
from app.core.schema import ChatMessage, UnifiedChatResponse, UnifiedChatRequest, UnifiedChoice, Usage


class _ScriptedEmbedder:
    """Maps specific message text to specific vectors so hit/miss outcomes
    are exact and deterministic -- "close" and "far" are picked by hand,
    not by hoping a real model agrees paraphrases are similar."""

    dimension = 4

    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self._vectors = vectors

    async def embed(self, text: str) -> list[float]:
        return self._vectors.get(text, [0.0, 0.0, 0.0, 1.0])  # unmapped text -> far away by default


def _settings(**overrides) -> GatewaySettings:
    defaults = dict(cache_enabled=True)
    defaults.update(overrides)
    return GatewaySettings(**defaults)


def _team(**overrides) -> TeamConfig:
    defaults = dict(team_id="data-science", api_key_hash="x", allowed_models=["openai:gpt-5.4"], semantic_cache_enabled=True)
    defaults.update(overrides)
    return TeamConfig(**defaults)


def _response(text: str, *, finish_reason: str = "stop") -> UnifiedChatResponse:
    return UnifiedChatResponse(
        id="r1",
        provider="fake",
        model_requested="openai:gpt-5.4",
        model_served="gpt-5.4-served",
        choices=[UnifiedChoice(index=0, message=ChatMessage(role="assistant", content=text), finish_reason=finish_reason)],
        usage=Usage(input_tokens=5, output_tokens=5),
    )


def _gw(embedder, **settings_overrides):
    r = fakeredis.FakeAsyncRedis(decode_responses=True, version=(8, 0, 0))
    exact = ExactMatchCache(r, ttl_seconds=3600)
    semantic = SemanticCache(r, ttl_seconds=3600, similarity_threshold=0.97, max_entries_per_scope=5000)
    return CacheGateway(exact_cache=exact, semantic_cache=semantic, embedding_provider=embedder, settings=_settings(**settings_overrides))


async def test_miss_then_write_through_then_exact_hit_on_byte_identical_request():
    req = UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="what is a token bucket?")])
    embedder = _ScriptedEmbedder({"what is a token bucket?": [1.0, 0.0, 0.0, 0.0]})
    gw = _gw(embedder)
    team = _team()

    first = await gw.lookup(request=req, team=team)
    assert first.layer == "miss"

    await gw.write_through(request=req, team=team, response=_response("a token bucket refills over time"), provider="openai", cost_usd=0.002)

    second = await gw.lookup(request=req, team=team)
    assert second.layer == "exact"
    assert second.response.choices[0].message.content == "a token bucket refills over time"
    assert second.original_provider == "openai"
    assert second.original_cost_usd == 0.002


async def test_a_paraphrase_hits_the_semantic_layer_not_the_exact_layer():
    original = UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="what is a token bucket?")])
    paraphrase = UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="explain token buckets to me")])
    embedder = _ScriptedEmbedder(
        {
            "what is a token bucket?": [1.0, 0.0, 0.0, 0.0],
            "explain token buckets to me": [0.999, 0.001, 0.0, 0.0],  # deliberately close, not identical
        }
    )
    gw = _gw(embedder)
    team = _team()

    await gw.write_through(request=original, team=team, response=_response("a token bucket refills over time"), provider="openai", cost_usd=0.002)

    hit = await gw.lookup(request=paraphrase, team=team)
    assert hit.layer == "semantic"
    assert hit.similarity is not None and hit.similarity >= 0.97
    assert hit.response.choices[0].message.content == "a token bucket refills over time"


async def test_a_parameter_mismatch_on_an_otherwise_similar_request_is_still_a_miss():
    """Sec 4.5: similarity substitutes for prompt-text distance only, never
    for generation parameters."""
    original = UnifiedChatRequest(
        model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="what is a token bucket?")], max_tokens=100
    )
    same_text_different_params = UnifiedChatRequest(
        model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="what is a token bucket?")], max_tokens=999
    )
    embedder = _ScriptedEmbedder({"what is a token bucket?": [1.0, 0.0, 0.0, 0.0]})
    gw = _gw(embedder)
    team = _team()

    await gw.write_through(request=original, team=team, response=_response("answer"), provider="openai", cost_usd=0.001)

    # Exact layer also misses (max_tokens is part of the exact fingerprint
    # too) -- falls through to semantic, which must ALSO refuse due to the
    # params_match gate despite embedding identically.
    hit = await gw.lookup(request=same_text_different_params, team=team)
    assert hit.layer == "miss"


async def test_a_truncated_response_is_never_written_to_either_cache_layer():
    req = UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="hi")])
    embedder = _ScriptedEmbedder({"hi": [1.0, 0.0, 0.0, 0.0]})
    gw = _gw(embedder)
    team = _team()

    await gw.write_through(request=req, team=team, response=_response("cut off mid-", finish_reason="length"), provider="openai", cost_usd=0.001)

    assert (await gw.lookup(request=req, team=team)).layer == "miss"


async def test_a_tool_calling_request_is_never_written_even_if_write_through_is_called_directly():
    """Defensive re-check inside write_through() itself (Sec 6's own
    module docstring) -- a future call site skipping the eligibility check
    before calling write_through must not silently cache a tool-context
    response."""
    from app.core.schema import ToolDefinition

    tool = ToolDefinition(name="get_weather", parameters={"type": "object"})
    req = UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="weather?")], tools=[tool])
    embedder = _ScriptedEmbedder({"weather?": [1.0, 0.0, 0.0, 0.0]})
    gw = _gw(embedder)
    team = _team()

    await gw.write_through(request=req, team=team, response=_response("sunny"), provider="openai", cost_usd=0.001)

    # Can't look it up through gw.lookup() (also ineligible), so check Redis directly.
    from app.caching.fingerprint import scope

    s = scope(team=team, requested_model=req.model)
    assert await gw._exact.get(scope=s, request=req) is None
