"""
Tier chain resolution and fallback execution (TRD, "Abstract Tier-Based
Fallback Chains"; Document 03, Journey B).

This is the module that ties circuit breaker + retry + provider adapters
together into the routing decision hierarchy Document 05 describes:

    1. Circuit State Check   -> app/resilience/circuit_breaker.py
    2. Primary Execution     -> the adapter's call()/stream()
    3. Error Evaluation      -> ProviderError.retryable
    4. Jittered Backoff      -> app/resilience/retry.py
    5. Failover Routing      -> advance to the next chain link

`request.model` (see app/core/schema.py) is still just a string — Phase 1
never changed shape, Phase 3 doesn't either. What's new is that the
string can now ALSO be a key in config/tiers.yaml. `resolve_chain()` is
the seam: a tier name resolves to its configured chain; anything else
(the Phase 1/2 literal "provider:model" case) resolves to a one-element
chain containing itself, so existing team configs and existing tests that
monkeypatch a single `resolve_model` keep working completely unchanged —
a one-link chain walk degrades to exactly the old direct-call behavior.

IMPORTANT — why `resolve_fn` is a parameter, not an import: every Phase
1/2 test that wants to substitute a FakeAdapter does
`monkeypatch.setattr("app.api.v1_chat.resolve_model", ...)`. If this
module imported `app.providers.registry.resolve_model` directly and
called it, that monkeypatch target would go stale the moment Phase 3
routes execution through here instead of straight through v1_chat.py.
Instead, v1_chat.py passes ITS OWN module-level `resolve_model` name
into `execute_non_streaming`/`stream_with_fallback` on every request —
since monkeypatch overwrites that name in v1_chat.py's module namespace,
and v1_chat.py re-reads it fresh on every request, the existing test seam
keeps working with zero test changes required for the common single-link
case.

PHASE 4 ADDITION — CLIENT spans live here, not in v1_chat.py: Document
05's worked trace example is a failed primary CLIENT span next to a
succeeded fallback CLIENT span, one per chain link. This module already
loops per link, so it's the natural, minimal-diff place for that
instrumentation. It's also why `execute_non_streaming` now returns the
already-*translated* `UnifiedChatResponse` instead of the provider's raw
dict: usage (`gen_ai.usage.input_tokens`/`.output_tokens`) has to be known
*before* the span closes to appear on it at all -- a span cannot be
enriched after `span.end()` runs, and by the time control returned to
v1_chat.py under the old raw-dict contract, the span had already closed.
Moving `adapter.translate_response(...)` in here (it only needs
`enriched_request` and `provider_model`, both already in scope) keeps
that enrichment inside the span's own `with` block. No test called
`execute_non_streaming` directly before this change (grepped to confirm),
so this is a safe, single-call-site contract change -- only
app/api/v1_chat.py's one call site needed updating.

`fallback_events_total{primary_provider,fallback_provider}` increments
once per actual link-to-link transition (including a transition into a
circuit-open skip, which Document 05 itself frames as "short-circuit
directly to fallback chain" -- that IS a failover trigger, just one with
no network call attached), not once per exhausted chain. A 3-link chain
where links 1 and 2 both fail before link 3 succeeds records two
transitions (1->2, 2->3), matching the metric's pairwise labels.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.schema import UnifiedChatRequest, UnifiedChatResponse, UnifiedStreamChunk
from app.observability.metrics import GatewayMetrics
from app.observability.tracing import (
    maybe_capture_content,
    provider_call_span,
    set_span_error,
    set_span_success,
)
from app.providers.base import ProviderAdapter, ProviderError
from app.providers.registry import UnknownProviderError
from app.resilience.circuit_breaker import CircuitBreaker
from app.resilience.health import HealthTracker
from app.resilience.retry import RetryPolicy

if TYPE_CHECKING:
    from app.core.config import TiersConfig

ResolveFn = Callable[[str], tuple[ProviderAdapter, str]]


@dataclass
class FallbackAttempt:
    """One line of the story of a chain walk — surfaced in the 503 body
    when every link is exhausted (Document 03: "Which providers were
    tried, last error per provider")."""

    link: str
    provider: str | None
    model: str | None
    outcome: str  # "unresolvable" | "circuit_open" | "error"
    detail: str

    def as_dict(self) -> dict:
        return {
            "link": self.link,
            "provider": self.provider,
            "model": self.model,
            "outcome": self.outcome,
            "detail": self.detail,
        }


class FallbackExhaustedError(Exception):
    """
    Every link in the chain was either circuit-open or exhausted its
    retries — none produced a response, and no non-retryable error
    short-circuited the walk (that case raises the original ProviderError
    instead — see the module docstring's routing hierarchy). Maps to
    Document 03's 503 status contract: "Entire fallback chain for the
    requested tier is exhausted... Which providers were tried, last error
    per provider."
    """

    def __init__(self, *, tier_or_model: str, chain: list[str], attempts: list[FallbackAttempt]) -> None:
        self.tier_or_model = tier_or_model
        self.chain = chain
        self.attempts = attempts
        tried = ", ".join(a.link for a in attempts) or "(no links)"
        super().__init__(f"fallback chain exhausted for '{tier_or_model}': tried [{tried}]")


class FallbackRouter:
    def __init__(
        self,
        *,
        circuit_breaker: CircuitBreaker,
        retry_policy: RetryPolicy,
        tiers_config: "TiersConfig",
        health_tracker: HealthTracker | None = None,
        clock=time.time,
        tracer=None,
        metrics: GatewayMetrics | None = None,
        capture_content: bool = False,
    ) -> None:
        self._circuit_breaker = circuit_breaker
        self._retry_policy = retry_policy
        self._tiers_config = tiers_config
        self._health_tracker = health_tracker
        self._clock = clock
        # tracer=None and metrics=None are the pre-Phase-4 defaults on
        # purpose: test_streaming_passthrough.py's `_redis_free_fallback_router()`
        # and any other direct `FallbackRouter(...)` construction that
        # predates Phase 4 keeps working with zero test changes -- every
        # span/metric call site below goes through `internal_span`/
        # `provider_call_span` (which yield None and no-op when tracer is
        # None) or an explicit `if self._metrics is not None` guard.
        self._tracer = tracer
        self._metrics = metrics
        self._capture_content = capture_content

    def resolve_chain(self, model_id: str) -> list[str]:
        """
        "tier-1-reasoning" -> ["openai:gpt-5.4", "anthropic:claude-sonnet-5", ...]
        "openai:gpt-5.4"   -> ["openai:gpt-5.4"]   (literal, single-link — Phase 1/2 behavior)
        """
        chain = self._tiers_config.chain_for(model_id)
        if chain:
            return list(chain)
        return [model_id]

    async def _record_outcome(self, *, provider: str, model: str, ok: bool, latency_ms: float) -> None:
        """
        Feeds both the circuit breaker (routing decisions) and, if wired
        up, passive health tracking (observability — see
        app/resilience/health.py's module docstring on why these are two
        separate, non-gating consumers of the same event). Called once
        per chain LINK per request — i.e. once per provider actually
        attempted, not once per individual retry inside that link, which
        matches the circuit breaker's own granularity (a link that failed
        twice then succeeded on retry 3 is one success, not two failures
        and a success).
        """
        if ok:
            await self._circuit_breaker.record_success(provider=provider, model=model)
        else:
            await self._circuit_breaker.record_failure(provider=provider, model=model)
        if self._health_tracker is not None:
            await self._health_tracker.record_outcome(
                provider=provider, model=model, ok=ok, latency_ms=latency_ms
            )

    def _record_fallback_event(self, primary_provider: str, fallback_provider: str) -> None:
        """See module docstring: one increment per link-to-link transition,
        including a transition into a circuit-open skip."""
        if self._metrics is not None:
            self._metrics.fallback_events_total.labels(
                primary_provider=primary_provider, fallback_provider=fallback_provider
            ).inc()

    # -- non-streaming ---------------------------------------------------------

    async def execute_non_streaming(
        self,
        *,
        chain: list[str],
        resolve_fn: ResolveFn,
        enriched_request: UnifiedChatRequest,
        tier_or_model: str,
        team_id: str | None = None,
    ) -> tuple[ProviderAdapter, str, UnifiedChatResponse]:
        """
        Walks `chain` in order. Returns (adapter, provider_model, unified)
        for whichever link succeeded -- `unified` is the already-
        translated `UnifiedChatResponse`, not the provider's raw dict (see
        module docstring on why that moved here in Phase 4). Raises:
          - `UnknownProviderError` if NOT ONE link in the chain resolved to a
            configured adapter (preserves the exact Phase 1/2 400 behavior
            for a literal request naming an unconfigured provider).
          - The original `ProviderError` immediately, unwrapped, if any link
            fails with a non-retryable error (Document 03: never falls back
            for those).
          - `FallbackExhaustedError` if every resolvable, non-circuit-open
            link retried and failed with only retryable errors.
        """
        attempts: list[FallbackAttempt] = []
        last_unknown_exc: UnknownProviderError | None = None
        resolved_any = False
        previous_failed_provider: str | None = None

        for link in chain:
            try:
                adapter, provider_model = resolve_fn(link)
            except UnknownProviderError as exc:
                last_unknown_exc = exc
                attempts.append(
                    FallbackAttempt(
                        link=link, provider=None, model=None, outcome="unresolvable", detail=str(exc)
                    )
                )
                continue
            resolved_any = True

            if previous_failed_provider is not None:
                self._record_fallback_event(previous_failed_provider, adapter.provider_name)

            allowed = await self._circuit_breaker.allow_request(
                provider=adapter.provider_name, model=provider_model
            )
            if not allowed:
                attempts.append(
                    FallbackAttempt(
                        link=link,
                        provider=adapter.provider_name,
                        model=provider_model,
                        outcome="circuit_open",
                        detail="circuit breaker is open — skipped without a network call",
                    )
                )
                # No CLIENT span here on purpose: Kind=CLIENT represents an
                # actual outbound call, and a circuit-open skip is
                # explicitly the case where none happens (Document 05:
                # "short-circuit directly to fallback chain"). The skip is
                # still visible via the 503 body's `attempts` list, the
                # gen_ai_circuit_breaker_state gauge, and the structured
                # log line circuit_breaker.py already emits on transition.
                previous_failed_provider = adapter.provider_name
                continue

            payload = adapter.translate_request(enriched_request, provider_model=provider_model)
            start = self._clock()
            with provider_call_span(
                self._tracer,
                provider_name=adapter.provider_name,
                request_model=provider_model,
                team_id=team_id,
            ) as span:
                try:
                    raw = await self._retry_policy.run(
                        lambda a=adapter, p=payload: a.call(p),
                        description=f"{adapter.provider_name}:{provider_model}",
                    )
                except ProviderError as exc:
                    latency_ms = (self._clock() - start) * 1000
                    await self._record_outcome(
                        provider=adapter.provider_name,
                        model=provider_model,
                        ok=False,
                        latency_ms=latency_ms,
                    )
                    set_span_error(span, error_type=exc.error_type, message=exc.message)
                    attempts.append(
                        FallbackAttempt(
                            link=link,
                            provider=adapter.provider_name,
                            model=provider_model,
                            outcome="error",
                            detail=f"{exc.error_type}: {exc.message}",
                        )
                    )
                    if not exc.retryable:
                        # Document 03: "a bad request is a bad request on
                        # every provider" — bubble up as-is, no further
                        # links tried.
                        raise
                    previous_failed_provider = adapter.provider_name
                    continue

                latency_ms = (self._clock() - start) * 1000
                await self._record_outcome(
                    provider=adapter.provider_name, model=provider_model, ok=True, latency_ms=latency_ms
                )
                unified = adapter.translate_response(
                    raw, request=enriched_request, provider_model=provider_model
                )
                set_span_success(span, response_model=unified.model_served, usage=unified.usage)
                maybe_capture_content(
                    span,
                    enabled=self._capture_content,
                    prompt_messages=[m.model_dump() for m in enriched_request.messages],
                    completion_text=unified.choices[0].message.content if unified.choices else None,
                )
            return adapter, provider_model, unified

        if not resolved_any:
            assert last_unknown_exc is not None
            raise last_unknown_exc
        raise FallbackExhaustedError(tier_or_model=tier_or_model, chain=chain, attempts=attempts)

    # -- streaming ---------------------------------------------------------------

    async def stream_with_fallback(
        self,
        *,
        chain: list[str],
        resolve_fn: ResolveFn,
        enriched_request: UnifiedChatRequest,
        tier_or_model: str,
        team_id: str | None = None,
    ) -> AsyncIterator[UnifiedStreamChunk]:
        """
        Async generator yielding `UnifiedStreamChunk`s from whichever chain
        link ends up serving the request.

        Fallback across providers only happens for a failure that occurs
        BEFORE any content chunk has reached the caller — i.e. while still
        connecting/authenticating to a given provider. This is a Phase 3
        scope decision, not an oversight: FastAPI's `StreamingResponse`
        commits HTTP headers (status 200) the moment the response starts,
        before this generator's first `yield` runs, so the client is
        already committed to a 200 by the time any content could exist —
        walking away from a provider that has already streamed partial
        content back to the client would corrupt the response. Once the
        first content chunk of an attempt has been yielded, a subsequent
        failure from that same provider surfaces as the existing Phase 1/2
        mid-stream `event: error` frame (see app/api/v1_chat.py), not a
        fallback — this mirrors exactly how a real SSE client would have
        to handle it: everything already sent stays sent.

        Retries DO apply before the first chunk of a given link (the
        provider connection can be retried with backoff, same as the
        non-streaming path) — only the fallback-to-next-provider decision
        is scoped to "no content sent yet".

        PHASE 4: the CLIENT span for a link that starts successfully stays
        open across the whole `yield first_chunk` / `async for chunk in
        agen` sequence, closing only in the `finally: await agen.aclose()`
        below — same lifetime as the upstream generator itself, since a
        Python `with` block tolerates `yield` inside it (this file already
        relies on that same property for `agen.aclose()`'s placement).
        `gen_ai.usage.*` is set incrementally as usage chunks arrive
        (unlike the non-streaming path, where the full `Usage` is known
        the moment the span closes) rather than all at once.
        """
        attempts: list[FallbackAttempt] = []
        last_unknown_exc: UnknownProviderError | None = None
        resolved_any = False
        previous_failed_provider: str | None = None

        for link in chain:
            try:
                adapter, provider_model = resolve_fn(link)
            except UnknownProviderError as exc:
                last_unknown_exc = exc
                attempts.append(
                    FallbackAttempt(
                        link=link, provider=None, model=None, outcome="unresolvable", detail=str(exc)
                    )
                )
                continue
            resolved_any = True

            if previous_failed_provider is not None:
                self._record_fallback_event(previous_failed_provider, adapter.provider_name)

            allowed = await self._circuit_breaker.allow_request(
                provider=adapter.provider_name, model=provider_model
            )
            if not allowed:
                attempts.append(
                    FallbackAttempt(
                        link=link,
                        provider=adapter.provider_name,
                        model=provider_model,
                        outcome="circuit_open",
                        detail="circuit breaker is open — skipped without a network call",
                    )
                )
                previous_failed_provider = adapter.provider_name
                continue

            payload = adapter.translate_request(enriched_request, provider_model=provider_model)
            start = self._clock()

            async def _start_link(a=adapter, p=payload, pm=provider_model):
                gen = a.stream(p, request=enriched_request, provider_model=pm)
                chunk = await gen.__anext__()
                return gen, chunk

            with provider_call_span(
                self._tracer,
                provider_name=adapter.provider_name,
                request_model=provider_model,
                team_id=team_id,
            ) as span:
                try:
                    agen, first_chunk = await self._retry_policy.run(
                        _start_link, description=f"{adapter.provider_name}:{provider_model} stream start"
                    )
                except StopAsyncIteration:
                    # Provider returned a stream with zero frames. Not an
                    # error — nothing to yield, nothing to fall back from.
                    await self._record_outcome(
                        provider=adapter.provider_name,
                        model=provider_model,
                        ok=True,
                        latency_ms=(self._clock() - start) * 1000,
                    )
                    set_span_success(span, response_model=provider_model, usage=None)
                    return
                except ProviderError as exc:
                    await self._record_outcome(
                        provider=adapter.provider_name,
                        model=provider_model,
                        ok=False,
                        latency_ms=(self._clock() - start) * 1000,
                    )
                    set_span_error(span, error_type=exc.error_type, message=exc.message)
                    attempts.append(
                        FallbackAttempt(
                            link=link,
                            provider=adapter.provider_name,
                            model=provider_model,
                            outcome="error",
                            detail=f"{exc.error_type}: {exc.message}",
                        )
                    )
                    if not exc.retryable:
                        raise
                    previous_failed_provider = adapter.provider_name
                    continue

                await self._record_outcome(
                    provider=adapter.provider_name,
                    model=provider_model,
                    ok=True,
                    latency_ms=(self._clock() - start) * 1000,
                )
                set_span_success(span, response_model=first_chunk.model_served, usage=first_chunk.usage)
                yield first_chunk
                try:
                    async for chunk in agen:
                        if chunk.usage is not None and span is not None:
                            span.set_attribute("gen_ai.usage.input_tokens", chunk.usage.input_tokens)
                            span.set_attribute("gen_ai.usage.output_tokens", chunk.usage.output_tokens)
                        yield chunk
                except ProviderError as exc:
                    # Mid-stream failure, content already sent to the client —
                    # record it for circuit-breaker/health purposes, but do NOT
                    # attempt another link (see the docstring above). Let it
                    # propagate to v1_chat.py's existing mid-stream error
                    # handling, unchanged from Phase 1/2.
                    await self._record_outcome(
                        provider=adapter.provider_name, model=provider_model, ok=False, latency_ms=0.0
                    )
                    set_span_error(span, error_type=exc.error_type, message=exc.message)
                    raise
                finally:
                    await agen.aclose()
            return

        if not resolved_any:
            assert last_unknown_exc is not None
            raise last_unknown_exc
        raise FallbackExhaustedError(tier_or_model=tier_or_model, chain=chain, attempts=attempts)
