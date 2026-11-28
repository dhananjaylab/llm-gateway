"""
test_cache_response_redaction.py

Verifies docs/PHASE9B_KICKOFF_SCOPING.md Sec 9 point 2: a response
containing PII-shaped text is redacted BEFORE being written to either cache
layer -- the model itself can echo/infer PII into its output, and that
text is what would be replayed to a future, possibly different, caller.
Reuses app.core.policy.redact_pii (already tested in
test_policy_injection.py against prompts) rather than a second redaction
path.
"""

from __future__ import annotations

import fakeredis

from app.caching.cache_gateway import CacheGateway
from app.caching.exact_cache import ExactMatchCache
from app.caching.semantic_cache import SemanticCache
from app.core.config import GatewaySettings, TeamConfig
from app.core.schema import ChatMessage, UnifiedChatResponse, UnifiedChatRequest, UnifiedChoice, Usage


class _FixedVectorEmbedder:
    dimension = 4

    async def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]


def _gw():
    r = fakeredis.FakeAsyncRedis(decode_responses=True, version=(8, 0, 0))
    exact = ExactMatchCache(r, ttl_seconds=3600)
    semantic = SemanticCache(r, ttl_seconds=3600, similarity_threshold=0.97, max_entries_per_scope=5000)
    settings = GatewaySettings(cache_enabled=True)
    return CacheGateway(exact_cache=exact, semantic_cache=semantic, embedding_provider=_FixedVectorEmbedder(), settings=settings)


def _team() -> TeamConfig:
    return TeamConfig(team_id="data-science", api_key_hash="x", allowed_models=["openai:gpt-5.4"], semantic_cache_enabled=True)


def _req() -> UnifiedChatRequest:
    return UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="who do I contact?")])


def _response_with_pii() -> UnifiedChatResponse:
    return UnifiedChatResponse(
        id="r1",
        provider="fake",
        model_requested="openai:gpt-5.4",
        model_served="gpt-5.4-served",
        choices=[
            UnifiedChoice(
                index=0,
                message=ChatMessage(
                    role="assistant", content="Reach jane.doe@example.com, SSN 456-78-9012, phone 555-019-9999."
                ),
                finish_reason="stop",
            )
        ],
        usage=Usage(input_tokens=5, output_tokens=10),
    )


async def test_pii_in_the_response_is_redacted_before_being_cached():
    gw = _gw()
    team = _team()
    req = _req()

    await gw.write_through(request=req, team=team, response=_response_with_pii(), provider="fake", cost_usd=0.001)

    hit = await gw.lookup(request=req, team=team)
    assert hit.layer == "exact"
    cached_text = hit.response.choices[0].message.content
    assert "jane.doe@example.com" not in cached_text
    assert "456-78-9012" not in cached_text
    assert "555-019-9999" not in cached_text
    assert "[REDACTED_EMAIL]" in cached_text
    assert "[REDACTED_SSN]" in cached_text
    assert "[REDACTED_PHONE]" in cached_text
