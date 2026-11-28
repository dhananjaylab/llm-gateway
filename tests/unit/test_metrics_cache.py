"""
test_metrics_cache.py

Verifies the four Phase 9b cache metrics increment/observe correctly
(docs/PHASE9B_KICKOFF_SCOPING.md Sec 8), independent of the rest of the
gateway -- GatewayMetrics is per-app state bound to its own
CollectorRegistry (see this module's own established pattern), so these
are exercised directly against a fresh build_metrics() call.
"""

from __future__ import annotations

from app.observability.metrics import build_metrics


def _value(counter_or_gauge, **labels) -> float:
    return counter_or_gauge.labels(**labels)._value.get()


def test_cache_requests_total_increments_by_team_and_layer():
    metrics = build_metrics()
    metrics.cache_requests_total.labels(team_id="data-science", layer="exact_hit").inc()
    metrics.cache_requests_total.labels(team_id="data-science", layer="exact_hit").inc()
    metrics.cache_requests_total.labels(team_id="data-science", layer="miss").inc()
    assert _value(metrics.cache_requests_total, team_id="data-science", layer="exact_hit") == 2
    assert _value(metrics.cache_requests_total, team_id="data-science", layer="miss") == 1


def test_cache_similarity_histogram_observes_every_lookup_attempt():
    metrics = build_metrics()
    metrics.cache_similarity.labels(team_id="data-science").observe(0.982)
    metrics.cache_similarity.labels(team_id="data-science").observe(0.6)
    total = metrics.cache_similarity.labels(team_id="data-science")._sum.get()
    assert total == 0.982 + 0.6


def test_cache_entries_gauge_reflects_layer_and_team():
    metrics = build_metrics()
    metrics.cache_entries.labels(team_id="data-science", layer="exact").set(12)
    metrics.cache_entries.labels(team_id="data-science", layer="semantic").set(34)
    assert metrics.cache_entries.labels(team_id="data-science", layer="exact")._value.get() == 12
    assert metrics.cache_entries.labels(team_id="data-science", layer="semantic")._value.get() == 34


def test_cache_savings_usd_total_accumulates_across_hits():
    metrics = build_metrics()
    metrics.cache_savings_usd_total.labels(team_id="data-science").inc(0.002)
    metrics.cache_savings_usd_total.labels(team_id="data-science").inc(0.0035)
    assert _value(metrics.cache_savings_usd_total, team_id="data-science") == 0.002 + 0.0035


def test_metrics_endpoint_registry_includes_all_four_new_metric_families():
    from prometheus_client import generate_latest

    metrics = build_metrics()
    metrics.cache_requests_total.labels(team_id="t", layer="exact_hit").inc()
    body = generate_latest(metrics.registry).decode()
    assert "gen_ai_cache_requests_total" in body
    assert "gen_ai_cache_similarity" in body
    assert "gen_ai_cache_entries" in body
    assert "gen_ai_cache_savings_usd_total" in body
