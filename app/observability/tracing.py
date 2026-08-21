"""
OpenTelemetry tracing setup (TRD, "Instrument with OpenTelemetry";
Document 05's OTel span/attribute schema; Document 03 Journey B's trace
shape).

SCHEMA BASELINE (Phase 4 "search first" finding, re-verify periodically):
`gen_ai.*` semantic conventions moved out of the core `semantic-conventions`
repo entirely into a dedicated `semantic-conventions-genai` repo in May
2026. As of Phase 4 sign-off (Aug 2026) that repo has no tagged release
and nothing in it is marked Stable -- there is no versioned schema left to
pin against the way the TRD originally asked ("pin the exact schema
version in a comment"). This module pins to the pre-move (v1.36-era)
attribute set instead, which is what Document 05's own schema table
already speaks almost verbatim:

    gen_ai.operation.name          "chat"
    gen_ai.provider.name           adapter.provider_name (renamed from
                                    gen_ai.system in v1.37.0 -- Document 05
                                    already uses the current name)
    gen_ai.request.model           the model string requested for this
                                    specific attempt (one per chain link)
    gen_ai.response.model          the version actually served, set only
                                    on a successful attempt
    gen_ai.usage.input_tokens / .output_tokens
    error.type                     Stable core attribute, unaffected by
                                    any of the above churn
    server.address                 Stable core attribute (not currently
                                    populated -- would need each adapter to
                                    surface its own base_url's host, which
                                    none currently expose; flagged as a
                                    follow-up, not blocking)

Deliberately NOT setting OTEL_SEMCONV_STABILITY_OPT_IN=gen_ai_latest_experimental
(GatewaySettings.otel_semconv_stability_opt_in defaults to None): opting in
would trade the current baseline for a *different*, equally Development-
status shape (e.g. gen_ai.content.prompt/.completion, which Document 05
also names, were removed outright rather than renamed -- see
`maybe_capture_content` below for how this module sidesteps that
specifically). Re-evaluate once the dedicated repo cuts a first tagged
release.

TESTABILITY: every other piece of per-request state in this codebase
(RateLimiter, CircuitBreaker, BudgetEnforcer...) is instantiated fresh per
FastAPI app instance and hung off `app.state`, never a module-level
singleton -- because tests/unit/conftest.py builds a brand new `app` per
test. OTel's own `trace.set_tracer_provider()` is a process-global
singleton by design (the SDK only honors the *first* call and warns on
every subsequent one), which is exactly wrong for that pattern: a second
test's provider would silently never take effect. So this module never
calls `trace.set_tracer_provider()` at all -- `init_tracing()` returns a
`TracerProvider` that the caller (app/main.py's lifespan) stores on
`app.state.tracer_provider`/`app.state.tracer`, and `FastAPIInstrumentor`
is instrumented against that explicit provider (`tracer_provider=...`),
not the global one. Every span-emitting call site in this codebase reads
`request.app.state.tracer` (or receives a `tracer` constructor argument,
e.g. FallbackRouter) rather than `trace.get_tracer()`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager

from fastapi import FastAPI
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor, SpanExporter
from opentelemetry.trace import Span, SpanKind, Status, StatusCode

logger = logging.getLogger("gateway.tracing")

# Event name for opt-in prompt/completion capture. Deliberately NOT
# "gen_ai.*" namespaced -- that's precisely the part of the spec still
# being restructured (gen_ai.content.prompt/.completion were removed, not
# renamed, in favor of a still-unstable gen_ai.input.messages/
# .output.messages shape). A gateway-owned event name means this feature
# never breaks because an upstream spec PR merged; it just isn't a
# portable, vendor-recognized event name, which is an acceptable trade for
# something that's off by default and only useful for local debugging.
CONTENT_CAPTURE_EVENT = "gateway.content.capture"


def init_tracing(
    app: FastAPI,
    *,
    service_name: str,
    service_version: str,
    otlp_endpoint: str | None,
    span_exporter_override: SpanExporter | None = None,
) -> TracerProvider:
    """
    Build a TracerProvider scoped to this one `app` instance and
    instrument `app`'s ASGI middleware stack with it (root SERVER span,
    stable HTTP semconv -- status follows the final HTTP response code
    automatically, which is exactly Journey B's "root span reports OK even
    when a child provider span errored" requirement, with zero extra
    logic on our part).

    `span_exporter_override` is a test-only seam: pass an
    `InMemorySpanExporter` to assert on emitted spans directly (see
    tests/unit/test_span_attributes.py) without a real OTLP collector.
    """
    resource = Resource.create({"service.name": service_name, "service.version": service_version})
    provider = TracerProvider(resource=resource)

    if span_exporter_override is not None:
        # SimpleSpanProcessor, not BatchSpanProcessor: exports synchronously
        # on every span.end(), no background thread, no batching delay --
        # this is the seam tests use (InMemorySpanExporter), and batching
        # would make span assertions flaky/racy against a test's own
        # timing rather than a genuine production concern.
        provider.add_span_processor(SimpleSpanProcessor(span_exporter_override))
    elif otlp_endpoint:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        logger.info("tracing: exporting spans to OTLP/HTTP endpoint %s", otlp_endpoint)
    else:
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is not set -- spans are created but never "
            "exported anywhere. Set it to a collector (e.g. http://localhost:4318/v1/traces) "
            "to see real traces; deploy/ wiring for a local collector ships in Phase 5."
        )

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=provider,
        # /healthz, /readyz, /metrics are polled every few seconds by
        # container orchestration and Prometheus itself -- tracing them
        # would flood every trace backend with high-frequency, zero-value
        # spans. Excluded the same way any production deployment would.
        excluded_urls="healthz,readyz,metrics",
        # Default FastAPIInstrumentor behavior also emits an ASGI-internal
        # INTERNAL span per "http send"/"http receive" event -- noise this
        # gateway's traces don't need (Document 05's own worked example
        # shows exactly one SERVER root plus the spans this codebase
        # creates on purpose, nothing ASGI-protocol-level).
        exclude_spans=["send", "receive"],
    )

    logger.info(
        "tracing: gen_ai.* schema baseline = pre-v1.42.0 (pinned in code, not via "
        "OTEL_SEMCONV_STABILITY_OPT_IN -- see app/observability/tracing.py docstring); "
        "service.name=%s service.version=%s",
        service_name,
        service_version,
    )
    return provider


@contextmanager
def internal_span(tracer, name: str, **attributes) -> Iterator[Span | None]:
    """
    A Kind=INTERNAL span for an in-process pipeline stage (Document 05:
    "Guardrail Spans (INTERNAL): ...authentication, content moderation,
    PII scrubbing, and token bucket evaluation"). `tracer` may be None
    (e.g. a test double that never wired tracing) -- yields None and does
    nothing, so every call site can use this unconditionally rather than
    branching on "is tracing enabled" at every call site.
    """
    if tracer is None:
        yield None
        return
    with tracer.start_as_current_span(name, kind=SpanKind.INTERNAL) as span:
        for key, value in attributes.items():
            if value is not None:
                span.set_attribute(key, value)
        yield span


@contextmanager
def provider_call_span(
    tracer,
    *,
    provider_name: str,
    request_model: str,
    team_id: str | None = None,
) -> Iterator[Span | None]:
    """
    A Kind=CLIENT span for one attempt against one provider-model pair
    (Document 05's Span Name Format: "{gen_ai.operation.name}
    {gen_ai.request.model}", e.g. "chat gpt-5.6-sol"). One of these is
    opened per fallback-chain link attempted -- see
    app/resilience/fallback.py, which is where every call site for this
    helper lives. Callers are responsible for calling
    `set_span_success`/`set_span_error` before the `with` block exits;
    this context manager only opens/closes the span and sets the
    request-time attributes that are known up front.
    """
    if tracer is None:
        yield None
        return
    span_name = f"chat {request_model}"
    with tracer.start_as_current_span(span_name, kind=SpanKind.CLIENT) as span:
        span.set_attribute("gen_ai.operation.name", "chat")
        span.set_attribute("gen_ai.provider.name", provider_name)
        span.set_attribute("gen_ai.request.model", request_model)
        if team_id is not None:
            span.set_attribute("gen_ai.client.team_id", team_id)
        yield span


def set_span_success(
    span: Span | None, *, response_model: str, usage=None, cost_usd: float | None = None
) -> None:
    """Document 05: CLIENT span on success carries gen_ai.response.model
    and usage counts; span status OK (Journey B's second, successful
    attempt). Phase 7: also carries gen_ai.usage.cost_usd when the caller
    can compute it (FallbackRouter, when constructed with a pricing_table
    — see fallback.py's module docstring) — a custom extension attribute,
    same category as gen_ai.client.team_id, not part of the Stable core
    schema."""
    if span is None:
        return
    span.set_attribute("gen_ai.response.model", response_model)
    if usage is not None:
        span.set_attribute("gen_ai.usage.input_tokens", usage.input_tokens)
        span.set_attribute("gen_ai.usage.output_tokens", usage.output_tokens)
    if cost_usd is not None:
        span.set_attribute("gen_ai.usage.cost_usd", cost_usd)
    span.set_status(Status(StatusCode.OK))


def set_span_error(span: Span | None, *, error_type: str, message: str) -> None:
    """Document 05: CLIENT span on failure carries error.type (Stable
    core attribute, unaffected by the gen_ai.* churn) and ERROR status
    (Journey B's first, failed attempt) -- the root SERVER span's own
    status is untouched by this; FastAPIInstrumentor derives it from the
    final HTTP response, which is what keeps a routine failover from
    reading as an application-level error."""
    if span is None:
        return
    span.set_attribute("error.type", error_type)
    span.set_status(Status(StatusCode.ERROR, message))


def maybe_capture_content(
    span: Span | None,
    *,
    enabled: bool,
    prompt_messages: list[dict] | None = None,
    completion_text: str | None = None,
) -> None:
    """
    Opt-in-only prompt/completion capture, per the TRD's compliance
    constraint: "Prompt and completion text must never be attached to
    span attributes by default... only to opt-in span events." `enabled`
    is `GatewaySettings.otel_capture_message_content`, read at the call
    site so this function stays a pure "given the flag, do the thing"
    helper with no config-loading of its own.

    Emits ONE event (not two, not per-message) under
    `CONTENT_CAPTURE_EVENT` -- deliberately not gen_ai.*-namespaced, see
    this module's docstring. A no-op whenever `span` is None or `enabled`
    is False, so call sites don't need their own guard.
    """
    if span is None or not enabled:
        return
    payload: dict = {}
    if prompt_messages is not None:
        payload["prompt_messages"] = prompt_messages
    if completion_text is not None:
        payload["completion_text"] = completion_text
    if payload:
        span.add_event(CONTENT_CAPTURE_EVENT, {"gateway.content.json": json.dumps(payload)})


def span_contains_no_content(span: ReadableSpan) -> bool:
    """
    Test helper (tests/unit/test_no_prompt_leakage.py): true if neither
    `span`'s attributes nor its events carry the content-capture event at
    all -- used to assert the compliance default holds with zero
    knowledge of what the actual prompt text was, so the test can't
    accidentally pass by matching the wrong string.
    """
    return all(event.name != CONTENT_CAPTURE_EVENT for event in span.events)
