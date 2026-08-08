"""
POST /v1/chat/completions and GET /v1/models.

Phase 2 pipeline (stages 1, 3, 5-7 unchanged from Phase 1; stage 4's
permissive stub is now real enforcement, and budget enforcement is
inserted before it per Document 03's Journey C: reject before any
upstream provider call is made, in cost order — budget first since it's
a read-only check, then rate limiting which mutates shared state):

  1. deserialize + validate (FastAPI/Pydantic)
  2. authenticate -> TeamConfig                    [resolve_team, now Redis-backed]
  3. enforce model allow-list                       [enforce_model_allowed]
  4a. budget precheck (fail-closed)                 [BudgetEnforcer.precheck]
  4b. rate limit check (Redis token bucket,          [RateLimiter.check /
      or batch priority queueing)                    BatchPriorityQueue.run_with_queueing]
  5. policy injection                                [apply_policy]
  6. circuit-breaker check (stub: always Closed)     [CircuitBreaker.allow_request]
  7. resolve provider + translate + execute          [registry.resolve_model, adapter]
  8. translate response, reconcile reservation,      [adapter, RateLimiter.reconcile,
     record actual spend                              BudgetEnforcer.record_spend]

Streaming/non-streaming dispatch logic (StreamingResponse vs. direct
return) is unchanged from Phase 1 — see the VERSION NOTE preserved below.
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
from app.resilience.stub import CircuitBreaker

logger = logging.getLogger("gateway.v1_chat")

router = APIRouter()

_circuit_breaker = CircuitBreaker()


def _provider_error_response(exc: ProviderError) -> dict:
    return {"error": {"type": exc.error_type, "message": exc.message}}


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

    try:
        adapter, provider_model = resolve_model(enriched_request.model)
    except UnknownProviderError as exc:
        # Reservation was made but the request can go no further — refund
        # it immediately rather than letting it sit until TTL.
        await rate_limiter.reconcile(team=team, reserved_tokens=estimated_tokens, actual_tokens=0)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"type": "unknown_provider", "message": str(exc)}},
        ) from exc

    if not await _circuit_breaker.allow_request(
        provider=adapter.provider_name, model=provider_model
    ):
        await rate_limiter.reconcile(team=team, reserved_tokens=estimated_tokens, actual_tokens=0)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": {
                    "type": "circuit_open",
                    "message": f"{adapter.provider_name}:{provider_model} circuit is open.",
                }
            },
        )

    payload = adapter.translate_request(enriched_request, provider_model=provider_model)

    if enriched_request.stream:
        return StreamingResponse(
            _stream_response(
                adapter=adapter,
                payload=payload,
                request=enriched_request,
                provider_model=provider_model,
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

    try:
        raw = await adapter.call(payload)
    except ProviderError as exc:
        await _circuit_breaker.record_failure(provider=adapter.provider_name, model=provider_model)
        await rate_limiter.reconcile(team=team, reserved_tokens=estimated_tokens, actual_tokens=0)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_provider_error_response(exc)
        ) from exc

    await _circuit_breaker.record_success(provider=adapter.provider_name, model=provider_model)
    unified = adapter.translate_response(raw, request=enriched_request, provider_model=provider_model)

    await rate_limiter.reconcile(
        team=team, reserved_tokens=estimated_tokens, actual_tokens=unified.usage.total_tokens
    )

    cost_usd = calculate_cost_usd(pricing_table, unified.provider, unified.model_served, unified.usage)
    budget_result = await budget_enforcer.record_spend(team, cost_usd)
    if budget_result.warning_fraction is not None:
        response.headers["X-Budget-Warning"] = f"{budget_result.warning_fraction:.2f}"

    return unified


async def _stream_response(
    *,
    adapter,
    payload: dict,
    request: UnifiedChatRequest,
    provider_model: str,
    http_request: Request,
    team: TeamConfig,
    reserved_tokens: int,
    rate_limiter,
    budget_enforcer,
    pricing_table,
):
    """
    Dual-pipeline streaming, unchanged from Phase 1 in shape: forward each
    normalized chunk to the client as soon as it arrives, while a client
    disconnect cancels the upstream call. New in Phase 2: the terminal
    usage chunk (once known) drives reservation reconciliation and actual
    budget spend recording — both best-effort with respect to the SSE
    stream itself (a Redis hiccup here must never surface as a broken
    stream to a client that already received a complete answer).
    """
    start = time.perf_counter()
    first_chunk_logged = False
    final_usage: Usage | None = None
    final_model_served = provider_model

    agen = adapter.stream(payload, request=request, provider_model=provider_model)
    try:
        async for chunk in agen:
            if await http_request.is_disconnected():
                logger.info(
                    "client disconnected mid-stream, cancelling upstream call",
                    extra={"team_id": team.team_id, "provider": adapter.provider_name},
                )
                break

            if not first_chunk_logged:
                ttft_ms = (time.perf_counter() - start) * 1000
                logger.info(
                    "time_to_first_token",
                    extra={
                        "team_id": team.team_id,
                        "provider": adapter.provider_name,
                        "model_served": chunk.model_served,
                        "ttft_ms": round(ttft_ms, 1),
                    },
                )
                first_chunk_logged = True

            if chunk.usage is not None:
                final_usage = chunk.usage
                final_model_served = chunk.model_served

            yield f"data: {chunk.model_dump_json()}\n\n"
    except ProviderError as exc:
        await _circuit_breaker.record_failure(provider=adapter.provider_name, model=provider_model)
        await rate_limiter.reconcile(team=team, reserved_tokens=reserved_tokens, actual_tokens=0)
        yield f"event: error\ndata: {json.dumps(_provider_error_response(exc))}\n\n"
        return
    else:
        await _circuit_breaker.record_success(provider=adapter.provider_name, model=provider_model)
        actual_tokens = final_usage.total_tokens if final_usage else 0
        await rate_limiter.reconcile(
            team=team, reserved_tokens=reserved_tokens, actual_tokens=actual_tokens
        )
        if final_usage is not None:
            cost_usd = calculate_cost_usd(
                pricing_table, adapter.provider_name, final_model_served, final_usage
            )
            await budget_enforcer.record_spend(team, cost_usd)
    finally:
        await agen.aclose()


@router.get("/v1/models")
async def list_models(team: TeamConfig = Depends(resolve_team)):
    """Models this team's key is authorized to call — resolved from its allow-list."""
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in team.allowed_models]}
