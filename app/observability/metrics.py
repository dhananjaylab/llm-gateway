"""
Prometheus metrics (TRD, "Export Prometheus metrics"; Document 05's
Prometheus metrics schema table -- the 9 metrics below, verbatim names
and labels).

Scoped to a per-app `CollectorRegistry`, never `prometheus_client`'s
process-global `REGISTRY`: every other piece of per-request state in this
codebase (RateLimiter, CircuitBreaker, BudgetEnforcer...) is already
instantiated fresh per FastAPI app instance and hung off `app.state`,
precisely so tests/unit/conftest.py's fresh-app-per-test fixture works. A
module-level `Counter`/`Histogram`/`Gauge` registered against the global
`REGISTRY` would raise "Duplicated timeseries in CollectorRegistry" the
moment a second test built a second app in the same process (each app
would try to register the same metric name again). `GatewayMetrics` is
that same per-app-state pattern applied to metrics; `build_metrics()` is
called once per app in app/main.py's lifespan.

HISTOGRAM NOTE (Phase 4 "search first" finding): Prometheus's own native
histograms have been server-side stable since v3.8, but `prometheus_client`'s
*authoring* side only recently grew narrow, OpenMetrics-2.0-content-
negotiation-only native-histogram exposition -- not the mature default
path the TRD assumed ("Prometheus 3.x's native histogram support is used
for the two latency histograms"). Both latency histograms below are
classic (pre-declared bucket boundaries) `Histogram`s instead, with
buckets hand-tuned for what this gateway actually measures (sub-10ms
gateway overhead up through a slow multi-second generation) rather than
prometheus_client's generic web-request defaults, which are too coarse
below 100ms -- exactly where this gateway's own P99 target lives.

CIRCUIT-STATE GAUGE NUMBERING -- READ BEFORE TOUCHING THIS FILE: Document
05 defines `gen_ai_circuit_breaker_state` as "0=Closed, 1=Half-Open,
2=Open". This is a DIFFERENT ordering from circuit_check.lua/
circuit_record.lua's own internal `state_code` (0=closed, 1=open,
2=half_open, per circuit_check.lua's own comment) -- the two numberings
swap 1 and 2. `circuit_state_to_gauge_value()` below is the ONE place
that conversion happens; every call site converts through the STATE
STRING ("closed"/"open"/"half_open") and this function, never through the
Lua layer's raw int, specifically so this swap can't silently invert in a
second call site.
"""

from __future__ import annotations

from dataclasses import dataclass

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

# Seconds. Deliberately not prometheus_client's default web-latency
# buckets (which are too coarse below 100ms) -- spans the gateway's own
# <10ms overhead SLA up through a slow, multi-second non-streamed
# generation.
_REQUEST_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20, 30, 60)
# TTFT is streaming-only and sub-second-focused -- finer low-end
# resolution than the full-request histogram above.
_TTFT_BUCKETS = (0.05, 0.1, 0.25, 0.5, 0.75, 1, 1.5, 2, 3, 5, 10)

# Document 05: "0=Closed, 1=Half-Open, 2=Open" -- see module docstring.
_CIRCUIT_GAUGE_VALUES = {"closed": 0, "half_open": 1, "open": 2}


def circuit_state_to_gauge_value(state: str) -> int:
    """`state` is the human string CircuitBreaker already uses
    ("closed"/"open"/"half_open"), never circuit_check.lua's raw
    state_code int -- see the module docstring's numbering warning."""
    return _CIRCUIT_GAUGE_VALUES.get(state, 0)


@dataclass
class GatewayMetrics:
    """One instance per app (`app.state.metrics`). Every metric is bound
    to `registry`, never prometheus_client's global `REGISTRY` -- see
    module docstring."""

    registry: CollectorRegistry
    requests_total: Counter
    token_usage_total: Counter
    cost_usd_total: Counter
    request_duration_seconds: Histogram
    time_to_first_token_seconds: Histogram
    fallback_events_total: Counter
    circuit_breaker_state: Gauge
    rate_limit_applied_total: Counter
    budget_applied_total: Counter
    # FLAGGED ADDITION, 10th metric beyond Document 05's literal table of
    # 9: none of those 9 make a PromQL "team approaching 80% of budget"
    # alert rule (the TRD's own Phase 4 build task: "team approaching
    # budget cap") actually expressible -- gen_ai_budget_applied_total
    # only increments on the *hard* 402 block, and gen_ai_cost_usd_total
    # has no per-team cap to divide against. This gauge is the minimal fix:
    # updated at the same point app/ratelimit/budget.py already computes
    # spend/cap for the X-Budget-Warning response header, so it's not new
    # bookkeeping, just also exporting a number that already existed.
    # See deploy/prometheus/alerts.yml's header for the resulting rule.
    budget_utilization_ratio: Gauge


def build_metrics(registry: CollectorRegistry | None = None) -> GatewayMetrics:
    registry = registry if registry is not None else CollectorRegistry()
    return GatewayMetrics(
        registry=registry,
        requests_total=Counter(
            "gen_ai_requests_total",
            "Total volume of requests routed through the gateway.",
            ["team_id", "provider", "model", "status"],
            registry=registry,
        ),
        token_usage_total=Counter(
            "gen_ai_token_usage_total",
            "Accumulated input, output, and cached token throughput.",
            ["team_id", "provider", "model", "type"],
            registry=registry,
        ),
        cost_usd_total=Counter(
            "gen_ai_cost_usd_total",
            "Cumulative spending in USD per team and model.",
            ["team_id", "provider", "model"],
            registry=registry,
        ),
        request_duration_seconds=Histogram(
            "gen_ai_server_request_duration_seconds",
            "End-to-end request latency distribution.",
            ["provider", "model", "status"],
            buckets=_REQUEST_DURATION_BUCKETS,
            registry=registry,
        ),
        time_to_first_token_seconds=Histogram(
            "gen_ai_server_time_to_first_token_seconds",
            "TTFT latency distribution for streaming calls.",
            ["provider", "model"],
            buckets=_TTFT_BUCKETS,
            registry=registry,
        ),
        fallback_events_total=Counter(
            "gen_ai_fallback_events_total",
            "Total frequency of failover execution triggers.",
            ["primary_provider", "fallback_provider"],
            registry=registry,
        ),
        circuit_breaker_state=Gauge(
            "gen_ai_circuit_breaker_state",
            "Current circuit state (0=Closed, 1=Half-Open, 2=Open).",
            ["provider", "model"],
            registry=registry,
        ),
        rate_limit_applied_total=Counter(
            "gen_ai_rate_limit_applied_total",
            "429s issued, by RPM vs. TPM.",
            ["team_id", "limit_type"],
            registry=registry,
        ),
        budget_applied_total=Counter(
            "gen_ai_budget_applied_total",
            "402/403s issued for budget exhaustion.",
            ["team_id"],
            registry=registry,
        ),
        budget_utilization_ratio=Gauge(
            "gen_ai_budget_utilization_ratio",
            "Current spend / cap for a team's active budget period (not one of Document 05's "
            "original 9 -- added to make an 80%-of-budget alert rule expressible; see this "
            "module's docstring).",
            ["team_id"],
            registry=registry,
        ),
    )


def record_token_usage_and_cost(
    metrics: GatewayMetrics | None,
    *,
    team_id: str,
    provider: str,
    model: str,
    usage,
    cost_usd: float,
) -> None:
    """Shared by the non-streaming and streaming response paths in
    app/api/v1_chat.py -- both know `usage`/`cost_usd` at the same
    logical point (right after budget's `record_spend`), so this avoids
    two independently-drifting call sites."""
    if metrics is None:
        return
    for token_type, count in (
        ("input", usage.input_tokens),
        ("output", usage.output_tokens),
        ("cache_read", usage.cache_read_input_tokens),
        ("cache_creation", usage.cache_creation_input_tokens),
    ):
        if count:
            metrics.token_usage_total.labels(
                team_id=team_id, provider=provider, model=model, type=token_type
            ).inc(count)
    if cost_usd:
        metrics.cost_usd_total.labels(team_id=team_id, provider=provider, model=model).inc(cost_usd)
