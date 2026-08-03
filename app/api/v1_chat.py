"""
POST /v1/chat/completions and GET /v1/models.

Pipeline for a chat completion request (stages 1, 3, 5-7 of the eight-stage
pipeline in Document 03 — auth is stage 2, rate limiting stage 4, and the
circuit-breaker check stage 6 are wired here but stubbed permissive-allow
until Phases 2 and 3):

  1. deserialize + validate (FastAPI/Pydantic, before this function runs)
  2. authenticate -> TeamConfig                          [resolve_team]
  3. enforce model allow-list                             [enforce_model_allowed]
  4. rate limit check (stub: always allowed)               [RateLimiter.check]
  5. policy injection (system prompt + PII redaction)      [apply_policy]
  6. circuit-breaker check (stub: always Closed)            [CircuitBreaker.allow_request]
  7. resolve provider + translate + execute                [registry.resolve_model, adapter]
  8. translate response back to the unified schema and return

VERSION NOTE (checked at build time per the TRD working agreement):
FastAPI 0.135+ ships a native `fastapi.sse` module (EventSourceResponse /
ServerSentEvent). It was evaluated for this endpoint and deliberately not
used: its SSE encoding only activates for a route whose path-operation
*function itself* is a generator, decided statically via
`response_class=EventSourceResponse` at route registration
(fastapi/routing.py checks `dependant.is_async_gen_callable`). This
endpoint streams or not based on the client's `stream` field in the
request *body*, which isn't known until after the function starts running
— a single function can't both `return` a JSON response and `yield` SSE
frames. The classic pattern below (a plain async generator manually
formatted as "data: ...\\n\\n" text, wrapped in StreamingResponse) is what
the TRD's Concise Implementation Guide specifies for exactly this reason,
and is what's used here.
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from app.core.auth import enforce_model_allowed, resolve_team
from app.core.config import TeamConfig
from app.core.policy import apply_policy
from app.core.schema import UnifiedChatRequest, UnifiedChatResponse
from app.providers.base import ProviderError
from app.providers.registry import UnknownProviderError, resolve_model
from app.ratelimit.stub import RateLimiter
from app.resilience.stub import CircuitBreaker

logger = logging.getLogger("gateway.v1_chat")

router = APIRouter()

_rate_limiter = RateLimiter()
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
    team: TeamConfig = Depends(resolve_team),
):
    enforce_model_allowed(team, request.model)

    rl_decision = await _rate_limiter.check(
        team_id=team.team_id,
        estimated_tokens=request.max_tokens,
        priority=request.priority,
    )
    if not rl_decision.allowed:  # always False from the Phase 1 stub today
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={"error": {"type": "rate_limit_exceeded", "message": "Rate limit exceeded."}},
            headers={"Retry-After": str(rl_decision.retry_after_seconds or 1)},
        )

    enriched_request = apply_policy(request, team)

    try:
        adapter, provider_model = resolve_model(enriched_request.model)
    except UnknownProviderError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": {"type": "unknown_provider", "message": str(exc)}},
        ) from exc

    if not await _circuit_breaker.allow_request(
        provider=adapter.provider_name, model=provider_model
    ):
        # Unreachable with the Phase 1 stub (always True); Phase 3 makes
        # this a real short-circuit-to-fallback branch.
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
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        raw = await adapter.call(payload)
    except ProviderError as exc:
        await _circuit_breaker.record_failure(provider=adapter.provider_name, model=provider_model)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=_provider_error_response(exc)
        ) from exc

    await _circuit_breaker.record_success(provider=adapter.provider_name, model=provider_model)
    return adapter.translate_response(raw, request=enriched_request, provider_model=provider_model)


async def _stream_response(
    *,
    adapter,
    payload: dict,
    request: UnifiedChatRequest,
    provider_model: str,
    http_request: Request,
    team: TeamConfig,
):
    """
    Dual-pipeline streaming: forward each normalized chunk to the client as
    soon as it arrives (no buffering the full completion), while a client
    disconnect cancels the upstream call rather than continuing to consume
    (and bill) tokens for a response nobody will receive.
    """
    start = time.perf_counter()
    first_chunk_logged = False

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

            yield f"data: {chunk.model_dump_json()}\n\n"
    except ProviderError as exc:
        await _circuit_breaker.record_failure(provider=adapter.provider_name, model=provider_model)
        yield f"event: error\ndata: {json.dumps(_provider_error_response(exc))}\n\n"
        return
    else:
        await _circuit_breaker.record_success(provider=adapter.provider_name, model=provider_model)
    finally:
        await agen.aclose()


@router.get("/v1/models")
async def list_models(team: TeamConfig = Depends(resolve_team)):
    """Models this team's key is authorized to call — resolved from its allow-list."""
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in team.allowed_models]}
