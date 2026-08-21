"""
POST /v1/chat/completions and GET /v1/models.

Phase 4 pipeline (stages 1-8 unchanged in shape from Phase 3; every stage
now also emits a span and/or a metric where Document 05 calls for one):

  1. deserialize + validate (FastAPI/Pydantic)     [root SERVER span, auto via FastAPIInstrumentor]
  2. authenticate -> TeamConfig                     [resolve_team, via Depends]
  3-4. allow-list + budget precheck + rate limit     [one combined "auth.rate_limit_check" INTERNAL span]
  5. policy injection                                [apply_policy]
  6. resolve tier -> fallback chain                  [FallbackRouter.resolve_chain]
  7. walk the chain: circuit check, retry, failover   [FallbackRouter.execute_non_streaming /
                                                        .stream_with_fallback -- one CLIENT span
                                                        per chain-link attempt, in fallback.py]
  8. reconcile reservation, record spend,             [RateLimiter.reconcile, BudgetEnforcer.record_spend,
     record request-level metrics                      GatewayMetrics.*, via app.observability.metrics]

Stage grouping for the combined INTERNAL span mirrors Document 05's own
worked trace example verbatim: "Span 1.1: Authentication & Rate Limit
Check (Kind: INTERNAL, Status: OK)" is ONE span in that example, not a
span per pipeline stage -- policy injection and provider selection
(stages 5-6) are cheap, in-memory, sub-millisecond operations not judged
worth their own spans at this phase's granularity; the CLIENT spans
around actual provider calls (stage 7, in fallback.py) are where the
real latency and failure information lives, and get the bulk of the
instrumentation effort.

PHASE 4 CONTRACT CHANGE: `FallbackRouter.execute_non_streaming` now
returns the already-translated `UnifiedChatResponse`, not the provider's
raw dict -- see app/resilience/fallback.py's module docstring for why
(the CLIENT span needs usage numbers before it closes, and translation is
what produces them). This function's own `adapter.translate_response(...)`
call is gone as a result; everything downstream of it is otherwise
unchanged.

Two behavioral changes from Phase 2, both flowing directly from the new
routing hierarchy (Document 05), carried forward from Phase 3 unchanged:

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
Phase 3/4 changes that trade-off.
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
from app.observability.metrics import record_token_usage_and_cost
from app.observability.tracing import internal_span
from app.providers.base import ProviderError
from app.providers.registry import UnknownProviderError, resolve_model
from app.ratelimit.budget import BudgetUnavailableError
from app.ratelimit.estimator import estimate_input_tokens, estimate_reserved_tokens
from app.ratelimit.output_tokenizer import count_partial_output_tokens
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


def _budget_exceeded_exception(team: TeamConfig, budget_decision) -> HTTPException:
    """Shared by both the batch-priority and combined-quota-check paths in
    chat_completions below (Phase 7 split these into two call sequences —
    see app/ratelimit/combined.py's module docstring on why) so the 402
    body's exact shape can't silently drift between the two."""
    return HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "error": {
                "type": "budget_exceeded",
                "message": (
                    f"Team '{team.team_id}' has reached its {team.budget_period} "
                    f"budget cap for {budget_decision.period}."
                ),
                "spend_usd": budget_decision.spend_usd,
                "cap_usd": budget_decision.cap_usd,
                "period": budget_decision.period,
            }
        },
    )


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
    app_state = http_request.app.state
    tracer = app_state.tracer
    metrics = app_state.metrics
    rate_limiter = app_state.rate_limiter
    budget_enforcer = app_state.budget_enforcer
    batch_queue = app_state.batch_queue
    combined_quota_checker = app_state.combined_quota_checker
    pricing_table = app_state.pricing
    fallback_router = app_state.fallback_router

    request_start = time.perf_counter()
    # Best-effort labels for the outer requests_total/duration recording
    # below -- overwritten with the real served pair once routing
    # succeeds; left at these defaults for every rejection that happens
    # before routing (401/403/402/429), which is the correct behavior:
    # those requests never touched a provider, so "" is accurate, not a
    # placeholder standing in for missing data.
    provider_label = ""
    model_label = request.model

    try:
        with internal_span(
            tracer, "auth.rate_limit_check", **{"gen_ai.client.team_id": team.team_id}
        ):
            enforce_model_allowed(team, request.model)

            # -- stage 4a+4b: budget + rate limit -----------------------------
            estimated_tokens = estimate_reserved_tokens(request)

            if request.priority == "batch":
                # Batch priority keeps its own separate budget precheck +
                # queueing loop (app/ratelimit/priority_queue.py) rather than
                # the Phase 7 combined round trip below -- that loop already
                # retries the rate-limit check itself over up to
                # BATCH_QUEUE_MAX_WAIT_SECONDS, which a single combined
                # Redis call isn't shaped for. See
                # app/ratelimit/combined.py's module docstring.
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
                    if metrics is not None:
                        metrics.budget_applied_total.labels(team_id=team.team_id).inc()
                    raise _budget_exceeded_exception(team, budget_decision)

                rl_decision = await batch_queue.run_with_queueing(
                    team=team, estimated_tokens=estimated_tokens, limiter=rate_limiter
                )
            else:
                # Phase 7: budget precheck + RPM/TPM check in ONE Redis round
                # trip on the healthy-Redis fast path (falls back to the
                # exact two original separate calls on a Redis outage,
                # preserving budget's fail-closed / rate-limit's fail-open
                # postures independently -- see combined.py).
                try:
                    combined = await combined_quota_checker.check(
                        team=team, estimated_tokens=estimated_tokens
                    )
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

                if combined.budget_denied:
                    if metrics is not None:
                        metrics.budget_applied_total.labels(team_id=team.team_id).inc()
                    raise _budget_exceeded_exception(team, combined.budget)

                rl_decision = combined.rate_limit

            if not rl_decision.allowed:
                if metrics is not None:
                    metrics.rate_limit_applied_total.labels(
                        team_id=team.team_id, limit_type=rl_decision.limit_type or "unknown"
                    ).inc()
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

        # -- stage 6: resolve the request's model/tier into an ordered chain
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
                    metrics=metrics,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                    "X-RateLimit-Remaining-RPM": str(rl_decision.remaining_rpm),
                    "X-RateLimit-Remaining-TPM": str(rl_decision.remaining_tpm),
                },
            )

        # -- stage 7: walk the fallback chain (circuit check, retry, failover)
        try:
            adapter, provider_model, unified = await fallback_router.execute_non_streaming(
                chain=chain,
                resolve_fn=resolve_model,
                enriched_request=enriched_request,
                tier_or_model=enriched_request.model,
                team_id=team.team_id,
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

        provider_label = adapter.provider_name
        model_label = provider_model

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

        record_token_usage_and_cost(
            metrics,
            team_id=team.team_id,
            provider=unified.provider,
            model=unified.model_served,
            usage=unified.usage,
            cost_usd=cost_usd,
        )
        if metrics is not None:
            duration_s = time.perf_counter() - request_start
            metrics.requests_total.labels(
                team_id=team.team_id, provider=provider_label, model=model_label, status="200"
            ).inc()
            metrics.request_duration_seconds.labels(
                provider=provider_label, model=model_label, status="200"
            ).observe(duration_s)

        return unified
    except HTTPException as exc:
        if metrics is not None:
            duration_s = time.perf_counter() - request_start
            metrics.requests_total.labels(
                team_id=team.team_id, provider=provider_label, model=model_label, status=str(exc.status_code)
            ).inc()
            metrics.request_duration_seconds.labels(
                provider=provider_label, model=model_label, status=str(exc.status_code)
            ).observe(duration_s)
        raise


async def _reconcile_and_bill_partial(
    *,
    team: TeamConfig,
    reserved_tokens: int,
    accumulated_text: str,
    final_usage: Usage | None,
    final_provider: str | None,
    final_model_served: str | None,
    tier_or_model: str,
    request: UnifiedChatRequest,
    rate_limiter,
    budget_enforcer,
    pricing_table,
    metrics,
) -> Usage | None:
    """
    Phase 7: shared by both `_stream_response`'s mid-stream-failure
    (`except`) and normal-completion (`else`) branches. Before this
    helper existed, "no terminal usage chunk ever arrived" — a client
    disconnect, or a provider failure after content had already reached
    the client — meant a blanket full TPM refund and
    `budget_enforcer.record_spend()` was never called at all: tokens the
    client actually received were never billed. See
    docs/PHASE7_IMPLEMENTATION_GUIDE.md ("Advanced Token Accounting for
    Cancelled and Aborted Streams").

    If the terminal usage chunk DID arrive, this is a byte-for-byte
    pass-through of the pre-Phase-7 behavior (uses `final_usage` exactly
    as before — nothing below changes for the common, uninterrupted case).

    Only when it's missing AND real content was generated
    (`final_provider is not None and accumulated_text` is truthy) does
    this compute a best-effort `Usage` from what's actually known: the
    same pre-flight input-token estimate already used to size the
    reservation (`app/ratelimit/estimator.py::estimate_input_tokens`,
    recomputed here rather than threaded through as an extra parameter —
    cheap, and keeps every existing call site's signature stable), and
    the partial output text via
    `app/ratelimit/output_tokenizer.py::count_partial_output_tokens`
    (tiktoken for OpenAI, Google's local tokenizer for Gemini, Anthropic's
    own documented heuristic for Anthropic, the project's existing
    heuristic for anything else).

    Returns the `Usage` actually used for accounting (so the caller can
    still record per-token-type Prometheus metrics), or `None` if there
    was truly nothing to bill.
    """
    if final_usage is not None:
        usage: Usage | None = final_usage
    elif final_provider is not None and accumulated_text:
        output_tokens = count_partial_output_tokens(
            final_provider, final_model_served or "", accumulated_text
        )
        usage = Usage(input_tokens=estimate_input_tokens(request), output_tokens=output_tokens)
    else:
        usage = None

    actual_tokens = usage.total_tokens if usage is not None else 0
    await rate_limiter.reconcile(team=team, reserved_tokens=reserved_tokens, actual_tokens=actual_tokens)

    if usage is not None and final_provider is not None:
        model_label = final_model_served or tier_or_model
        cost_usd = calculate_cost_usd(pricing_table, final_provider, model_label, usage)
        await budget_enforcer.record_spend(team, cost_usd)
        record_token_usage_and_cost(
            metrics,
            team_id=team.team_id,
            provider=final_provider,
            model=model_label,
            usage=usage,
            cost_usd=cost_usd,
        )

    return usage


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
    metrics=None,
):
    """
    Dual-pipeline streaming: forward each normalized chunk to the client
    as soon as it arrives, while a client disconnect cancels the upstream
    call. The terminal usage chunk (once known) drives reservation
    reconciliation and actual budget spend recording — both best-effort
    with respect to the SSE stream itself.

    Phase 3: `agen` is `fallback_router.stream_with_fallback(...)` instead
    of a single adapter's `.stream(...)` directly — everything below this
    line is otherwise unchanged from Phase 1/2, including the exception
    handling: `stream_with_fallback` re-raises `ProviderError` for exactly
    the same two cases Phase 1/2 already handled (a non-retryable
    failure, or a post-first-chunk mid-stream failure after fallback has
    already committed to a provider), so the `except ProviderError`
    branch below needs no changes to keep producing the same
    `event: error` SSE frame. A `FallbackExhaustedError` (every link
    failed before any content was sent) is the case Phase 3 adds here —
    surfaced the same way, since headers already committed a 200 and an
    SSE error frame is the only channel left to report it on.

    Phase 4: `status="streaming_error"` (not a real HTTP code — the
    client already received a 200 status line before any of this ran) is
    the label this function uses on `requests_total`/
    `request_duration_seconds` for the SSE-level failure case, so a
    dashboard can distinguish a genuinely failed streaming request from a
    successful one without both collapsing into a misleading "200".

    Phase 7: `accumulated_text` is what makes
    `_reconcile_and_bill_partial` (above) possible — every delta actually
    forwarded to the client is appended to it, so if the stream is cut
    short (disconnect or mid-stream provider failure), there's a
    best-effort record of exactly what the client received to estimate
    output tokens from, instead of the old "assume zero" behavior.
    """
    start = time.perf_counter()
    first_chunk_logged = False
    final_usage: Usage | None = None
    final_provider = None
    final_model_served = None
    accumulated_text = ""

    agen = fallback_router.stream_with_fallback(
        chain=chain,
        resolve_fn=resolve_model,
        enriched_request=request,
        tier_or_model=tier_or_model,
        team_id=team.team_id,
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
                ttft_s = time.perf_counter() - start
                logger.info(
                    "time_to_first_token",
                    extra={
                        "team_id": team.team_id,
                        "provider": chunk.provider,
                        "model_served": chunk.model_served,
                        "ttft_ms": round(ttft_s * 1000, 1),
                    },
                )
                if metrics is not None:
                    metrics.time_to_first_token_seconds.labels(
                        provider=chunk.provider, model=chunk.model_served
                    ).observe(ttft_s)
                first_chunk_logged = True

            final_provider = chunk.provider
            final_model_served = chunk.model_served
            if chunk.usage is not None:
                final_usage = chunk.usage
            if chunk.delta:
                accumulated_text += chunk.delta

            yield f"data: {chunk.model_dump_json()}\n\n"
    except (ProviderError, FallbackExhaustedError) as exc:
        await _reconcile_and_bill_partial(
            team=team,
            reserved_tokens=reserved_tokens,
            accumulated_text=accumulated_text,
            final_usage=final_usage,
            final_provider=final_provider,
            final_model_served=final_model_served,
            tier_or_model=tier_or_model,
            request=request,
            rate_limiter=rate_limiter,
            budget_enforcer=budget_enforcer,
            pricing_table=pricing_table,
            metrics=metrics,
        )
        if metrics is not None:
            duration_s = time.perf_counter() - start
            metrics.requests_total.labels(
                team_id=team.team_id,
                provider=final_provider or "",
                model=final_model_served or tier_or_model,
                status="streaming_error",
            ).inc()
            metrics.request_duration_seconds.labels(
                provider=final_provider or "",
                model=final_model_served or tier_or_model,
                status="streaming_error",
            ).observe(duration_s)
        if isinstance(exc, FallbackExhaustedError):
            yield f"event: error\ndata: {json.dumps(_fallback_exhausted_response(exc))}\n\n"
        else:
            yield f"event: error\ndata: {json.dumps(_provider_error_response(exc))}\n\n"
        return
    else:
        await _reconcile_and_bill_partial(
            team=team,
            reserved_tokens=reserved_tokens,
            accumulated_text=accumulated_text,
            final_usage=final_usage,
            final_provider=final_provider,
            final_model_served=final_model_served,
            tier_or_model=tier_or_model,
            request=request,
            rate_limiter=rate_limiter,
            budget_enforcer=budget_enforcer,
            pricing_table=pricing_table,
            metrics=metrics,
        )
        if metrics is not None:
            duration_s = time.perf_counter() - start
            metrics.requests_total.labels(
                team_id=team.team_id,
                provider=final_provider or "",
                model=final_model_served or tier_or_model,
                status="200",
            ).inc()
            metrics.request_duration_seconds.labels(
                provider=final_provider or "", model=final_model_served or tier_or_model, status="200"
            ).observe(duration_s)
    finally:
        await agen.aclose()


@router.get("/v1/models")
async def list_models(team: TeamConfig = Depends(resolve_team)):
    """Models this team's key is authorized to call — resolved from its allow-list."""
    return {"object": "list", "data": [{"id": m, "object": "model"} for m in team.allowed_models]}
