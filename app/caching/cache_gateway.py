"""
CacheGateway -- the single entry point `app/api/v1_chat.py` calls (Phase 9b,
docs/PHASE9B_KICKOFF_SCOPING.md Sec 3/6). Owns the eligibility gate (Sec 4.3
double opt-in, Sec 4.4 temperature gating, Sec 4.5 streaming/tool-call
exclusion) and the exact-then-semantic decision tree; `ExactMatchCache` and
`SemanticCache` never talk to each other or know about eligibility -- this
is the one place that composes them, the same separation-of-concerns this
codebase already applies to `RateLimiter`+`BudgetEnforcer` vs.
`CombinedQuotaChecker`.

Returns a `CacheLookupResult` the same shape `RateLimitDecision`/
`BudgetDecision` already establish, rather than a bare bool or None, so a
caller can distinguish *why* nothing was served (ineligible vs. exact-miss
vs. semantic-miss) for logging/metrics without re-deriving it.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Literal

from app.caching.embedding import EmbeddingProvider
from app.caching.exact_cache import ExactMatchCache
from app.caching.fingerprint import canonical_text, params_match, scope
from app.caching.semantic_cache import SemanticCache
from app.core.config import GatewaySettings, TeamConfig
from app.core.policy import redact_pii
from app.core.schema import UnifiedChatRequest, UnifiedChatResponse

logger = logging.getLogger("gateway.caching.gateway")

CacheLayer = Literal["exact", "semantic", "miss", "ineligible"]


@dataclass
class CacheLookupResult:
    layer: CacheLayer
    response: UnifiedChatResponse | None = None
    similarity: float | None = None
    scope: str | None = None
    original_provider: str | None = None
    original_model_served: str | None = None
    original_cost_usd: float | None = None


def _ineligibility_reason(request: UnifiedChatRequest, team: TeamConfig, settings: GatewaySettings) -> str | None:
    """Returns the first reason the request can't participate, or None if
    eligible. Ordered cheapest-check-first so a request that's obviously
    ineligible (e.g. streaming) never even evaluates the rest -- and, more
    importantly, never touches Redis or the embedder."""
    if not settings.cache_enabled:
        return "gateway_cache_disabled"
    if not team.semantic_cache_enabled:
        return "team_cache_disabled"
    if request.stream:
        return "streaming"
    if request.tools:
        return "tool_calling"
    if request.temperature is not None and request.temperature > settings.cache_max_temperature:
        return "temperature_too_high"
    return None


class CacheGateway:
    def __init__(
        self,
        *,
        exact_cache: ExactMatchCache,
        semantic_cache: SemanticCache,
        embedding_provider: EmbeddingProvider,
        settings: GatewaySettings,
    ) -> None:
        self._exact = exact_cache
        self._semantic = semantic_cache
        self._embedder = embedding_provider
        self._settings = settings

    async def lookup(self, *, request: UnifiedChatRequest, team: TeamConfig) -> CacheLookupResult:
        """FAIL-OPEN BY DESIGN: a cache is an optimization, never a new way
        for a request to fail. Any error below the eligibility gate -- the
        embedding model failing to download/load (real, verified: see
        embedding.py's own docstring), Redis being unreachable, a
        malformed stored entry -- is logged loudly and degrades to a plain
        `miss`, so the request simply proceeds down the normal pipeline
        exactly as if caching didn't exist. `asyncio.CancelledError` is a
        BaseException, not an Exception, so a cancelled request still
        propagates correctly."""
        reason = _ineligibility_reason(request, team, self._settings)
        if reason is not None:
            return CacheLookupResult(layer="ineligible")
        try:
            return await self._lookup_impl(request=request, team=team)
        except Exception:
            logger.warning("cache lookup failed -- failing open to a miss", exc_info=True)
            return CacheLookupResult(layer="miss")

    async def _lookup_impl(self, *, request: UnifiedChatRequest, team: TeamConfig) -> CacheLookupResult:

        request_scope = scope(team=team, requested_model=request.model)

        if self._settings.cache_exact_match_enabled:
            exact_hit = await self._exact.get(scope=request_scope, request=request)
            if exact_hit is not None:
                return _result_from_payload(exact_hit, layer="exact", scope_value=request_scope)

        if not self._settings.cache_semantic_enabled:
            return CacheLookupResult(layer="miss", scope=request_scope)

        vector = await self._embedder.embed(canonical_text(request))
        semantic_hit = await self._semantic.get(scope=request_scope, vector=vector)
        if semantic_hit is None:
            return CacheLookupResult(layer="miss", scope=request_scope)

        payload, similarity = semantic_hit
        if not params_match(request, payload.get("request_params", {})):
            # Similarity substitutes for prompt-text distance only (Sec 4.5)
            # -- a parameter mismatch is a miss no matter how close the
            # embedding scored.
            return CacheLookupResult(layer="miss", scope=request_scope, similarity=similarity)

        return _result_from_payload(payload, layer="semantic", scope_value=request_scope, similarity=similarity)

    async def write_through(
        self,
        *,
        request: UnifiedChatRequest,
        team: TeamConfig,
        response: UnifiedChatResponse,
        provider: str,
        cost_usd: float,
    ) -> None:
        """Fail-open wrapper (same reasoning as lookup()): by the time this
        runs the provider call already succeeded and billing is recorded --
        a cache write failing must never turn that into a failed request."""
        try:
            await self._write_through_impl(
                request=request, team=team, response=response, provider=provider, cost_usd=cost_usd
            )
        except Exception:
            logger.warning("cache write-through failed -- response already served, skipping", exc_info=True)

    async def _write_through_impl(
        self,
        *,
        request: UnifiedChatRequest,
        team: TeamConfig,
        response: UnifiedChatResponse,
        provider: str,
        cost_usd: float,
    ) -> None:
        """Called only after a genuinely successful, non-streaming
        completion -- eligibility (including `finish_reason == "stop"`) is
        the caller's job (Sec 9 point 4: never cache a truncated,
        content-filtered, or errored response), checked again here
        defensively so a future call site can't skip it by mistake."""
        reason = _ineligibility_reason(request, team, self._settings)
        if reason is not None:
            return
        if not response.choices or response.choices[0].finish_reason != "stop":
            return

        request_scope = scope(team=team, requested_model=request.model)
        redacted_content = redact_pii(response.choices[0].message.content)

        payload = {
            "content": redacted_content,
            "finish_reason": response.choices[0].finish_reason,
            "usage": response.usage.model_dump(),
            "provider": provider,
            "model_served": response.model_served,
            "cost_usd": cost_usd,
            "created_at": time.time(),
            "request_params": {
                "max_tokens": request.max_tokens,
                "temperature": request.temperature,
                "top_p": request.top_p,
                "stop": request.stop,
                "response_format": request.response_format.model_dump() if request.response_format else None,
            },
        }

        if self._settings.cache_exact_match_enabled:
            await self._exact.set(scope=request_scope, request=request, payload=payload)

        if self._settings.cache_semantic_enabled:
            vector = await self._embedder.embed(canonical_text(request))
            await self._semantic.set(scope=request_scope, vector=vector, payload=payload)

    async def purge_team(self, *, team: TeamConfig, requested_models: list[str]) -> None:
        """Sec 7 / Sec 9 point 6: the incident-response path. Purges every
        scope the given team could plausibly have written to -- one call
        per (requested_model) the team is allowed to use, since scope is
        keyed by (team, requested_model, policy) and the policy fingerprint
        is derived from the team's CURRENT policy, which is exactly what an
        operator wants purged after a policy or content incident."""
        for model in requested_models:
            request_scope = scope(team=team, requested_model=model)
            await self._exact.purge_scope(scope=request_scope)
            await self._semantic.purge_scope(scope=request_scope)


def _result_from_payload(
    payload: dict, *, layer: CacheLayer, scope_value: str, similarity: float | None = None
) -> CacheLookupResult:
    from app.core.schema import ChatMessage, UnifiedChoice, Usage

    response = UnifiedChatResponse(
        id=UnifiedChatResponse.new_id(),
        provider=f"cache:{payload['provider']}",
        model_requested=payload["model_served"],
        model_served=payload["model_served"],
        choices=[
            UnifiedChoice(
                index=0,
                message=ChatMessage(role="assistant", content=payload["content"]),
                finish_reason=payload["finish_reason"],
            )
        ],
        usage=Usage(**payload["usage"]),
    )
    return CacheLookupResult(
        layer=layer,
        response=response,
        similarity=similarity,
        scope=scope_value,
        original_provider=payload["provider"],
        original_model_served=payload["model_served"],
        original_cost_usd=payload["cost_usd"],
    )
