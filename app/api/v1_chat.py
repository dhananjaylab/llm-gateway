"""
POST /v1/chat/completions and GET /v1/models.

Phase 3 pipeline (stages 1-5 unchanged from Phase 2; stage 6's permissive
stub is now the real Redis-backed circuit breaker, reached through the
fallback router rather than called directly, and stage 7 can now mean
"walk a multi-provider chain with retries", not just "call one adapter"):

  1. deserialize + validate (FastAPI/Pydantic)
  2. authenticate -> TeamConfig
  3. enforce model allow-list                       [enforce_model_allowed]
  4a. budget precheck (fail-closed)                  [BudgetEnforcer.precheck]
  4b. rate limit check (Redis token bucket,          [RateLimiter.check /
      or batch priority queueing)                     BatchPriorityQueue.run_with_queueing]
  5. policy injection                                [apply_policy]
  6. resolve tier -> fallback chain                  [FallbackRouter.resolve_chain]
  7. walk the chain: circuit check, retry w/ backoff, [FallbackRouter.execute_non_streaming /
     failover to next link                            .stream_with_fallback]
  8. translate response, reconcile reservation,       [adapter, RateLimiter.reconcile,
     record actual spend, record passive health        BudgetEnforcer.record_spend,
                                                         HealthTracker.record_outcome (inside
                                                         the fallback router)]

Two behavioral changes from Phase 2, both flowing directly from the new
routing hierarchy (Document 05):

- A request that used to bubble up as a flat 502 on adapter failure can
  now surface as a 503 with a structured "which providers were tried"
  body (`FallbackExhaustedError`) once retries+fallback are exhausted —
  this only changes observable behavior for requests whose model resolves
  to more than a trivially-exhausted single link, or where retries were
  attempted; a single always-failing adapter with the historic
  single-link-only chain now also gets retried up to
  `RETRY_MAX_ATTEMPTS` times before that 503, where Phase 2 failed fast
  on the first error.
- A non-retryable provider error (401/400/403) still surfaces as a 502
  immediately (Document 03's HTTP status contract table lists 502 for a
  provider-side failure bubbled as-is) — that part is unchanged; what's
  new is that Phase 3 GUARANTEES no fallback was attempted for it,
  whereas Phase 2 had no fallback concept to begin with.

VERSION NOTE (unchanged from Phase 1): still a hand-formatted
`StreamingResponse`, not `fastapi.sse.EventSourceResponse` — see the
original Phase 1 reasoning preserved in git history; nothing about
Phase 3 changes that trade-off.
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from app.core.auth import enforce_model_allowed, resolve_team
from app.core.config import TeamConfig
from app.core.policy import apply_policy
from app.core.pricing import calculate_cost_usd
from app.core.schema import UnifiedChatRequest, UnifiedChatResponse, Usage
from app.providers.base import ProviderError
from app.providers.registry import UnknownProviderError, resolve_model
from app.ratelimit.budget import BudgetUnavailableError
from app.ratelimit.estimator import estimate_reserved_tokens
from app.resilience.fallback import FallbackExhaustedError

logger = logging.getLogger("gateway.v1_chat")

router = APIRouter()


def _provider_error_response(exc: ProviderError) -> dict:
    return {"error": {"type": exc.error_type, "message": exc.message}}


def _fallback_exhausted_response(exc: FallbackExhaustedError) -> dict:
    return {
        "error": {
            "type": "fallback_chain_exhausted",
            "message": str(exc),
            "tier_or_model": exc.tier_or_model,
            "chain": exc.chain,
            "attempts": [a.as_dict() for a in exc.attempts],
        }
    }


@router.post(
    "/v1/chat/completions",
    response_model=None,
    responses={200: {"model": UnifiedChatResponse}},
)
async def chat_completions(
    request: UnifiedChatRequest,
    http_request: Request,
    response: Response,
    team: TeamConfig = Depends(resolve_team),
):
    enforce_model_allowed(team, request.model)

    app_state = http_request.app.state
    rate_limiter = app_state.rate_limiter
    budget_enforcer = app_state.budget_enforcer
    batch_queue = app_state.batch_queue
    pricing_table = app_state.pricing
    fallback_router = app_state.fallback_router

    # -- stage 4a: budget precheck (fail-closed; read-only, so a rejection
    # here never needs to roll anything back) ------------------------------
    try:
        budget_decision = await budget_enforcer.precheck(team)
    except BudgetUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": {
                    "type": "budget_check_unavailable",
                    "message": "Budget ledger is temporarily unavailable; failing closed.",
                }
            },
        ) from exc

    if not budget_decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={
                "error": {
                    "type": "budget_exceeded",
                    "message": (
                        f"Team '{team.team_id}' has reached its {team.budget_period} budget "
                        f"cap for {budget_decision.period}."
                    ),
                    "spend_usd": budget_decision.spend_usd,
                    "cap_usd": budget_decision.cap_usd,
                    "period": budget_decision.period,
                }
            },
        )

    # -- stage 4b: rate limit check (or batch queueing) ---------------------
    estimated_tokens = estimate_reserved_tokens(request)

    if request.priority == "batch":
        rl_decision = await batch_queue.run_with_queueing(
            team=team, estimated_tokens=estimated_tokens, limiter=rate_limiter
        )
    else:
        rl_decision = await rate_limiter.check(
            team=team, estimated_tokens=estimated_tokens, priority=request.priority
        )

    if not rl_decision.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": {
                    "type": "rate_limit_exceeded",
                    "message": f"Rate limit exceeded on {rl_decision.limit_type or 'rpm/tpm'}.",
                    "limit_type": rl_decision.limit_type,
                    "remaining_rpm": rl_decision.remaining_rpm,
                    "remaining_tpm": rl_decision.remaining_tpm,
                }
            },
            headers={"Retry-After": str(rl_decision.retry_after_seconds or 1)},
        )

    response.headers["X-RateLimit-Remaining-RPM"] = str(rl_decision.remaining_rpm)
    response.headers["X-RateLimit-Remaining-TPM"] = str(rl_decision.remaining_tpm)

    enriched_request = apply_policy(request, team)

    # -- stage 6: resolve the request's model/tier into an ordered chain ----
    # A literal "provider:model" id (Phase 1/2 style) resolves to a
    # one-element chain containing itself — see FallbackRouter.resolve_chain.
    chain = fallback_router.resolve_chain(enriched_request.model)

    if enriched_request.stream:
        return StreamingResponse(
            _stream_response(
                fallback_router=fallback_router,
                chain=chain,
                tier_or_model=enriched_request.model,
                request=enriched_request,
                http_request=http_request,
                team=team,
                reserved_tokens=estimated_tokens,
                rate_limiter=rate_limiter,
                budget_enforcer=budget_enforcer,
                pricing_table=pricing_table,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "X-RateLimit-Remaining-RPM": str(rl_decision.remaining_rpm),
                "X-RateLimit-Remaining-TPM": str(rl_decision.remaining_tpm),
            },
        )

    # -- stage 7: walk the fallback chain (circuit check, retry, failover) --
    try:
        adapter, provider_model, raw = await fallback_router.execute_non_streaming(
            chain=chain,
            resolve_fn=resolve_model,
            enriched_request=enriched_request,
            tier_or_model=enriched_request.model,
        )
    except UnknownProviderError as exc:
        # Reservation was made but the request can go no further — refund
        # it immediately rather than letting it sit until TTL. Identical
        # to Phase 1/2's behavior for a literal unconfigured-provider
        # request (a one-link chain that fails to resolve raises this
        # same exception type).
        await rate_limiter.reconcile(team=team, reserved_tokens=estimated_tokens, actual_tokens=0)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"type": "unknown_provider", "message": str(exc)}},
        ) from exc
    except ProviderError as exc:
        # A non-retryable error bubbled straight through the chain walk —
        # Document 03: never retried, never triggers fallback.
        await rate_limiter.reconcile(team=team, reserved_tokens=estimated_tokens, actual_tokens=0)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_provider_error_response(exc)
        ) from exc
    except FallbackExhaustedError as exc:
        await rate_limiter.reconcile(team=team, reserved_tokens=estimated_tokens, actual_tokens=0)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=_fallback_exhausted_response(exc),
        ) from exc

    unified = adapter.translate_response(raw, request=enriched_request, provider_model=provider_model)

    await rate_limiter.reconcile(
        team=team, reserved_tokens=estimated_tokens, actual_tokens=unified.usage.total_tokens
    )

    cost_usd = calculate_cost_usd(pricing_table, unified.provider, unified.model_served, unified.usage)
    budget_result = await budget_enforcer.record_spend(team, cost_usd)
    if budget_result.warning_fraction is not None:
        response.headers["X-Budget-Warning"] = f"{budget_result.warning_fraction:.2f}"
    # Which provider:model actually served this — meaningful now that it
    # isn't necessarily the one literally named in the request.
    response.headers["X-Gateway-Served-Model"] = f"{adapter.provider_name}:{provider_model}"

    return unified


async def _stream_response(
    *,
    fallback_router,
    chain: list[str],
    tier_or_model: str,
    request: UnifiedChatRequest,
    http_request: Request,
    team: TeamConfig,
    reserved_tokens: int,
    rate_limiter,
    budget_enforcer,
    pricing_table,
):
    """
    Dual-pipeline streaming: forward each normalized chunk to the client
    as soon as it arrives, while a client disconnect cancels the upstream
    call. The terminal usage chunk (once known) drives reservation
    reconciliation and actual budget spend recording — both best-effort
    with respect to the SSE stream itself.

    Phase 3 change: `agen` is now `fallback_router.stream_with_fallback(...)`
    instead of a single adapter's `.stream(...)` directly — everything
    below this line is otherwise unchanged from Phase 1/2, including the
    exception handling: `stream_with_fallback` re-raises `ProviderError`
    for exactly the same two cases Phase 1/2 already handled (a
    non-retryable failure, or — new in Phase 3 — a post-first-chunk
    mid-stream failure after fallback has already committed to a
    provider), so the `except ProviderError` branch below needs no
    changes to keep producing the same `event: error` SSE frame. A
    `FallbackExhaustedError` (every link failed before any content was
    sent) is the one new case Phase 3 adds here — surfaced the same way,
    since headers already committed a 200 and an SSE error frame is the
    only channel left to report it on.
    """
    start = time.perf_counter()
    first_chunk_logged = False
    final_usage: Usage | None = None
    final_provider = None
    final_model_served = None

    agen = fallback_router.stream_with_fallback(
        chain=chain,
        resolve_fn=resolve_model,
        enriched_request=request,
        tier_or_model=tier_or_model,
    )
    try:
        async for chunk in agen:
            if await http_request.is_disconnected():
                logger.info(
                    "client disconnected mid-stream, cancelling upstream call",
                    extra={"team_id": team.team_id, "provider": chunk.provider},
                )
                break

            if not first_chunk_logged:
                ttft_ms = (time.perf_counter() - start) * 1000
                logger.info(
                    "time_to_first_token",
                    extra={
                        "team_id": team.team_id,
                        "provider": chunk.provider,
                        "model_served": chunk.model_served,
                        "ttft_ms": round(ttft_ms, 1),
                    },
                )
                first_chunk_logged = True

            final_provider = chunk.provider
            final_model_served = chunk.model_served
            if chunk.usage is not None:
                final_usage = chunk.usage

            yield f"data: {chunk.model_dump_json()}\n\n"
    except (ProviderError, FallbackExhaustedError) as exc:
        actual_tokens = final_usage.total_tokens if final_usage else 0
        await rate_limiter.reconcile(team=team, reserved_tokens=reserved_tokens, actual_tokens=actual_tokens)
        if isinstance(exc, FallbackExhaustedError):
            yield f"event: error\ndata: {json.dumps(_fallback_exhausted_response(exc))}\n\n"
        else:
            yield f"event: error\ndata: {json.dumps(_provider_error_response(exc))}\n\n"
        return
    else:
        actual_tokens = final_usage.total_tokens if final_usage else 0
        await rate_limiter.reconcile(
            team=team, reserved_tokens=reserved_tokens, actual_tokens=actual_tokens
        )
        if final_usage is not None and final_provider is not None:
            cost_usd = calculate_cost_usd(
                pricing_table, final_provider, final_model_served, final_usage
            )
            await budget_enforcer.record_spend(team, cost_usd)
    finally:
        await agen.aclose()


@router.get("/v1/models")
async def list_models(team: TeamConfig = Depends(resolve_team)):
    """Models this team's key is authorized to call — resolved from its allow-list."""
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in team.allowed_models]}
