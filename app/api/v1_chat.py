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

PHASE 9B ADDITIONS -- READ BEFORE TOUCHING THE TOP OF chat_completions():
Two-layer response cache (app/caching/*) checked between auth and
rate-limit/budget consumption. Two structural changes from Phase 1-9a,
both deliberate, both covered in docs/PHASE9B_KICKOFF_SCOPING.md:

1. `apply_policy(request, team)` now runs INSIDE the existing
   "auth.rate_limit_check" span, immediately after `enforce_model_allowed`
   -- i.e. earlier than Phase 1-9a, which ran it AFTER the whole rate-
   limit/budget block. This is so the cache key and any cached response
   body are always derived from the REDACTED text, never the raw one
   (Sec 2). `enforce_model_allowed` deliberately stays exactly where it
   was, first thing inside the span, so a disallowed-model 403 still
   reports on "auth.rate_limit_check" exactly as
   test_span_attributes.py's
   test_internal_auth_span_reports_error_on_a_403_model_not_allowed
   already asserts -- that test needed zero changes.
2. The cache lookup itself is a CHILD span ("cache.lookup") NESTED inside
   "auth.rate_limit_check", not a sibling span before it -- a refinement
   made during implementation, not the kickoff doc's literal proposal:
   nesting means every existing span-shape assertion for
   "auth.rate_limit_check" (the 403 case above, and the successful-request
   case in test_internal_auth_span_wraps_every_successful_request) keeps
   passing completely unchanged, while still achieving Sec 2's actual
   goal -- the cache lookup runs, and can short-circuit the whole request,
   before any rate-limit/budget capacity is ever touched. See Sec 9 for
   the attribute schema this span carries.

On a cache HIT, the function returns immediately from inside these nested
`with` blocks -- ordinary, well-supported Python; both spans close
normally (not as an error) since this is a return, not a raise. No CLIENT
span is emitted for a hit, mirroring the exact rule
app/resilience/fallback.py already applies to a skipped, open-circuit
call: Kind=CLIENT means an actual outbound call happened, and a cache hit
means it deliberately didn't.

On a cache MISS, `estimated_tokens` is now computed from `enriched_request`
(the redacted text), not the raw `request` -- a small, incidental
correction that falls out of computing it after redaction instead of
before: redaction can change text length (PII replaced with a shorter
placeholder), so estimating off the text that's actually sent to the
provider is strictly more accurate than before.

After a successful, non-streaming completion, `cache_gateway.write_through()`
is called unconditionally (a direct `await`, not a background task, per
this project's own "start simple" posture) -- eligibility AND
`finish_reason == "stop"` are both re-checked inside `write_through()`
itself, so this call site doesn't need to duplicate that logic, and a
future call site can't accidentally skip it either.

`cache_gateway` is read via `getattr(app_state, "cache_gateway", None)`
rather than a direct attribute access -- defensive robustness for any
app.state built without the full lifespan (a narrow test harness, e.g.),
not this feature's actual on/off switch: the real on/off switch is
`GatewaySettings.cache_enabled` / `TeamConfig.semantic_cache_enabled`,
both checked inside `CacheGateway` itself. See app/main.py's own note on
why `cache_gateway` is unconditionally constructed there.
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse

from app.caching.cache_gateway import CacheGateway
from app.core.auth import enforce_model_allowed, resolve_team
from app.core.config import OrgConfig, TeamConfig
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


def _org_budget_exceeded_exception(org: OrgConfig, budget_decision) -> HTTPException:
    """
    Phase 8, Option A (docs/PHASE8_KICKOFF_SCOPING.md §6): the org-level
    counterpart of _budget_exceeded_exception, distinguished in the body
    by `"level": "org"` and `org_id` so client-side error handling can
    tell an org-wide block apart from this team's own 402 without any
    other signal — Document 03's own HTTP-contract philosophy ("every
    non-2xx response must be self-explanatory") applied one level up.
    """
    return HTTPException(
        status_code=status.HTTP_402_PAYMENT_REQUIRED,
        detail={
            "error": {
                "type": "budget_exceeded",
                "level": "org",
                "message": (
                    f"Org '{org.org_id}' has reached its {org.budget_period} "
                    f"budget cap for {budget_decision.period}."
                ),
                "org_id": org.org_id,
                "spend_usd": budget_decision.spend_usd,
                "cap_usd": budget_decision.cap_usd,
                "period": budget_decision.period,
            }
        },
    )


def _org_rate_limited_exception(org: OrgConfig, rl_decision) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "error": {
                "type": "rate_limit_exceeded",
                "level": "org",
                "message": f"Org rate limit exceeded on {rl_decision.limit_type or 'rpm/tpm'}.",
                "org_id": org.org_id,
                "limit_type": f"org_{rl_decision.limit_type}" if rl_decision.limit_type else None,
                "remaining_rpm": rl_decision.remaining_rpm,
                "remaining_tpm": rl_decision.remaining_tpm,
            }
        },
        headers={"Retry-After": str(rl_decision.retry_after_seconds or 1)},
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
    org_store = app_state.org_store
    cache_gateway: CacheGateway | None = getattr(app_state, "cache_gateway", None)

    request_start = time.perf_counter()
    # Best-effort labels for the outer requests_total/duration recording
    # below -- overwritten with the real served pair once routing
    # succeeds; left at these defaults for every rejection that happens
    # before routing (401/403/402/429), which is the correct behavior:
    # those requests never touched a provider, so "" is accurate, not a
    # placeholder standing in for missing data.
    provider_label = ""
    model_label = request.model
    # Resolved inside the span below, referenced by every later
    # reconcile/record_spend call site (success path, non-streaming error
    # paths, and passed into the streaming helper) -- see module
    # docstring's Phase 8 note. `None` only if the team's org_id has no
    # matching OrgConfig row in Redis at all (not expected in practice —
    # OrgConfigStore always seeds "default-org" — but handled as "skip
    # org-level enforcement for this request" rather than a 500).
    org: OrgConfig | None = None

    try:
        with internal_span(
            tracer, "auth.rate_limit_check", **{"gen_ai.client.team_id": team.team_id}
        ):
            enforce_model_allowed(team, request.model)

            # Phase 9b: policy injection now runs HERE -- see module
            # docstring's "PHASE 9B ADDITIONS" section for why this moved
            # earlier than Phase 1-9a, and why enforce_model_allowed
            # deliberately did NOT move.
            enriched_request = apply_policy(request, team)

            # Phase 9b: cache lookup, nested inside this same span -- see
            # module docstring. A hit returns straight out of here, before
            # any rate-limit/budget capacity is touched.
            if cache_gateway is not None:
                with internal_span(
                    tracer, "cache.lookup", **{"gen_ai.client.team_id": team.team_id}
                ) as cache_span:
                    cache_result = await cache_gateway.lookup(request=enriched_request, team=team)
                    if cache_span is not None:
                        cache_span.set_attribute("gen_ai.gateway.cache.layer", cache_result.layer)
                        if cache_result.scope is not None:
                            cache_span.set_attribute("gen_ai.gateway.cache.scope", cache_result.scope)
                        if cache_result.similarity is not None:
                            cache_span.set_attribute(
                                "gen_ai.gateway.cache.similarity", cache_result.similarity
                            )

                    if metrics is not None and cache_result.similarity is not None:
                        metrics.cache_similarity.labels(team_id=team.team_id).observe(cache_result.similarity)
                    if metrics is not None:
                        layer_label = (
                            f"{cache_result.layer}_hit"
                            if cache_result.layer in ("exact", "semantic")
                            else cache_result.layer
                        )
                        metrics.cache_requests_total.labels(team_id=team.team_id, layer=layer_label).inc()

                    if cache_result.layer in ("exact", "semantic"):
                        # No CLIENT span here on purpose -- see fallback.py's
                        # own identical rule for a circuit-open skip: the
                        # skip/hit is visible via cache.layer on THIS span
                        # and the counter above, not a CLIENT span for a
                        # provider call that never happened.
                        cached_response = cache_result.response
                        response.headers["X-Gateway-Cache"] = "hit"
                        response.headers["X-Gateway-Served-Model"] = (
                            f"{cached_response.provider}:{cached_response.model_served}"
                        )
                        if metrics is not None:
                            if cache_result.original_cost_usd:
                                metrics.cache_savings_usd_total.labels(team_id=team.team_id).inc(
                                    cache_result.original_cost_usd
                                )
                            duration_s = time.perf_counter() - request_start
                            metrics.requests_total.labels(
                                team_id=team.team_id,
                                provider=cached_response.provider,
                                model=cached_response.model_served,
                                status="200",
                            ).inc()
                            metrics.request_duration_seconds.labels(
                                provider=cached_response.provider,
                                model=cached_response.model_served,
                                status="200",
                            ).observe(duration_s)
                        return cached_response

            # Phase 9b: estimated off enriched_request (the redacted text
            # actually sent onward), not the raw request -- see module
            # docstring's note on this incidental accuracy improvement.
            estimated_tokens = estimate_reserved_tokens(enriched_request)

            org = await org_store.get_org(team.org_id)
            if org is None:
                logger.warning(
                    "team=%s has org_id=%s but no matching OrgConfig exists in Redis — "
                    "skipping org-level enforcement for this request",
                    team.team_id,
                    team.org_id,
                )
            else:
                try:
                    org_budget_decision = await budget_enforcer.precheck_org(org)
                except BudgetUnavailableError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={
                            "error": {
                                "type": "budget_check_unavailable",
                                "level": "org",
                                "message": "Org budget ledger is temporarily unavailable; failing closed.",
                            }
                        },
                    ) from exc

                if not org_budget_decision.allowed:
                    if metrics is not None:
                        metrics.budget_applied_total.labels(team_id=team.team_id).inc()
                    raise _org_budget_exceeded_exception(org, org_budget_decision)

                org_rl_decision = await rate_limiter.check_org(org, estimated_tokens)
                if not org_rl_decision.allowed:
                    if metrics is not None:
                        metrics.rate_limit_applied_total.labels(
                            team_id=team.team_id,
                            limit_type=f"org_{org_rl_decision.limit_type or 'unknown'}",
                        ).inc()
                    raise _org_rate_limited_exception(org, org_rl_decision)

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

        # Phase 9b: enriched_request was already computed above, before the
        # cache lookup -- no second apply_policy() call here; policy
        # injection must only ever run once per request.

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
                    org=org,
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
            if org is not None:
                await rate_limiter.reconcile_org(org=org, reserved_tokens=estimated_tokens, actual_tokens=0)
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": {"type": "unknown_provider", "message": str(exc)}},
            ) from exc
        except ProviderError as exc:
            # A non-retryable error bubbled straight through the chain walk —
            # Document 03: never retried, never triggers fallback.
            await rate_limiter.reconcile(team=team, reserved_tokens=estimated_tokens, actual_tokens=0)
            if org is not None:
                await rate_limiter.reconcile_org(org=org, reserved_tokens=estimated_tokens, actual_tokens=0)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY, detail=_provider_error_response(exc)
            ) from exc
        except FallbackExhaustedError as exc:
            await rate_limiter.reconcile(team=team, reserved_tokens=estimated_tokens, actual_tokens=0)
            if org is not None:
                await rate_limiter.reconcile_org(org=org, reserved_tokens=estimated_tokens, actual_tokens=0)
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
        if org is not None:
            await rate_limiter.reconcile_org(
                org=org, reserved_tokens=estimated_tokens, actual_tokens=unified.usage.total_tokens
            )
            await budget_enforcer.record_spend_org(org, cost_usd)
        if budget_result.warning_fraction is not None:
            response.headers["X-Budget-Warning"] = f"{budget_result.warning_fraction:.2f}"
        # Which provider:model actually served this — meaningful now that it
        # isn't necessarily the one literally named in the request.
        response.headers["X-Gateway-Served-Model"] = f"{adapter.provider_name}:{provider_model}"

        if cache_gateway is not None:
            # Phase 9b write-through -- see module docstring. write_through()
            # re-checks eligibility and finish_reason == "stop" defensively
            # on its own, so this call site doesn't duplicate that logic.
            await cache_gateway.write_through(
                request=enriched_request,
                team=team,
                response=unified,
                provider=adapter.provider_name,
                cost_usd=cost_usd,
            )

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
    org: OrgConfig | None = None,
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

    Note: streaming responses never reach the Phase 9b cache (see
    app/caching/cache_gateway.py's eligibility gate -- `request.stream`
    is checked before anything else) -- this function has no cache
    interaction to add, on either branch, and needed zero changes for
    Phase 9b.

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
    if org is not None:
        await rate_limiter.reconcile_org(
            org=org, reserved_tokens=reserved_tokens, actual_tokens=actual_tokens
        )

    if usage is not None and final_provider is not None:
        model_label = final_model_served or tier_or_model
        cost_usd = calculate_cost_usd(pricing_table, final_provider, model_label, usage)
        await budget_enforcer.record_spend(team, cost_usd)
        if org is not None:
            await budget_enforcer.record_spend_org(org, cost_usd)
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
    org: OrgConfig | None = None,
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

    Phase 8b: `accumulated_text` also absorbs streamed tool-call argument
    fragments (`chunk.tool_call_deltas[].arguments_delta`), not just plain
    text `chunk.delta` — a disconnect or mid-stream failure while a tool
    call's arguments are still accumulating is real generated output the
    same way partial text is, and should feed the same best-effort
    output-token estimate rather than silently under-billing to zero for
    the tool-call portion.

    Phase 9b: unmodified. Streaming requests are ineligible for the cache
    on both the read and write sides (see app/caching/cache_gateway.py's
    eligibility gate) -- this function's body has nothing to add.
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
            # Phase 8b (docs/PHASE8B_KICKOFF_SCOPING.md §4): tool-call
            # argument fragments are real generated output too — folding
            # them into the same accumulated_text the char-ratio/tiktoken
            # heuristic in output_tokenizer.py already estimates from
            # closes the same class of financial-leakage gap Phase 7 fixed
            # for plain text, for a disconnect or mid-stream failure that
            # happens while a tool call's arguments are still streaming.
            if chunk.tool_call_deltas:
                for tool_delta in chunk.tool_call_deltas:
                    accumulated_text += tool_delta.arguments_delta

            yield f"data: {chunk.model_dump_json()}\n\n"
    except (ProviderError, FallbackExhaustedError) as exc:
        await _reconcile_and_bill_partial(
            team=team,
            org=org,
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
            org=org,
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
