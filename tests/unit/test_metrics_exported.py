"""
test_metrics_exported.py

Verifies the Phase 4 test plan: "Each of the nine Prometheus metrics
increments/updates correctly after a scripted sequence of successful,
rate-limited, and failed-over requests" -- plus GET /metrics itself
(status, content-type, and that it actually reflects app.state.metrics'
registry rather than some other one).

Reads values directly off `client.app.state.metrics` (a `GatewayMetrics`
dataclass instance, one per app -- see app/observability/metrics.py's
module docstring for why this is per-app state, not
prometheus_client's global REGISTRY) rather than parsing the /metrics
text body, except in the one test whose whole point IS that text body.
"""

from __future__ import annotations

import pytest

from app.observability.metrics import circuit_state_to_gauge_value
from tests.unit.conftest import BATCH_DEVS_KEY, DATA_SCIENCE_KEY, FakeAdapter


def _labels_value(counter_or_gauge, **labels) -> float:
    return counter_or_gauge.labels(**labels)._value.get()


def test_metrics_endpoint_serves_the_apps_own_registry(client):
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    # A metric that's always present at boot (declared, even if 0 samples
    # recorded yet) -- proves this endpoint is reading app.state.metrics'
    # actual registry, not an empty/default one.
    assert "gen_ai_requests_total" in resp.text
    assert "gen_ai_circuit_breaker_state" in resp.text


def test_requests_total_and_duration_increment_on_success(client, monkeypatch):
    fake = FakeAdapter(response_text="ok")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "gpt-5.4-served"))

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )
    assert resp.status_code == 200

    metrics = client.app.state.metrics
    assert (
        _labels_value(
            metrics.requests_total,
            team_id="data-science",
            provider="fake",
            model="gpt-5.4-served",
            status="200",
        )
        == 1
    )
    duration_samples = metrics.request_duration_seconds.labels(
        provider="fake", model="gpt-5.4-served", status="200"
    )._sum.get()
    assert duration_samples > 0


def test_requests_total_records_a_pre_routing_rejection_with_empty_provider(client):
    """batch-devs is only allowed ollama:llama3.2 -- this 403 never
    reaches routing, so provider="" per v1_chat.py's documented default."""
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Gateway-API-Key": BATCH_DEVS_KEY},
    )
    assert resp.status_code == 403

    metrics = client.app.state.metrics
    assert (
        _labels_value(
            metrics.requests_total, team_id="batch-devs", provider="", model="openai:gpt-5.4", status="403"
        )
        == 1
    )


def test_token_usage_and_cost_totals_increment_with_real_pricing(client, monkeypatch, admin_headers):
    from app.core.pricing import ModelPricing
    from app.core.schema import Usage

    fake = FakeAdapter(usage_override=Usage(input_tokens=1000, output_tokens=200))
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))
    client.app.state.pricing["fake:served-model"] = ModelPricing(
        input_per_million=2.0, output_per_million=10.0
    )

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )
    assert resp.status_code == 200

    metrics = client.app.state.metrics
    assert (
        _labels_value(
            metrics.token_usage_total,
            team_id="data-science",
            provider="fake",
            model="served-model",
            type="input",
        )
        == 1000
    )
    assert (
        _labels_value(
            metrics.token_usage_total,
            team_id="data-science",
            provider="fake",
            model="served-model",
            type="output",
        )
        == 200
    )
    # cost = 1000 * 2.0/1e6 + 200 * 10.0/1e6 = 0.002 + 0.002 = 0.004
    cost = _labels_value(
        metrics.cost_usd_total, team_id="data-science", provider="fake", model="served-model"
    )
    assert cost == pytest.approx(0.004)


def test_rate_limit_applied_total_increments_by_limit_type(client, monkeypatch, admin_headers):
    fake = FakeAdapter(response_text="ok")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))
    client.patch("/admin/limits/data-science", json={"rpm_cap": 1}, headers=admin_headers)

    body = {"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]}
    headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}
    first = client.post("/v1/chat/completions", json=body, headers=headers)
    second = client.post("/v1/chat/completions", json=body, headers=headers)
    assert first.status_code == 200
    assert second.status_code == 429

    metrics = client.app.state.metrics
    assert _labels_value(metrics.rate_limit_applied_total, team_id="data-science", limit_type="rpm") == 1


def test_budget_applied_total_increments_on_a_402(client, monkeypatch, admin_headers):
    from app.core.pricing import ModelPricing
    from app.core.schema import Usage

    client.patch("/admin/budgets/data-science", json={"budget_cap_usd": 1.0}, headers=admin_headers)
    fake = FakeAdapter(usage_override=Usage(input_tokens=1_000_000, output_tokens=0))
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))
    client.app.state.pricing["fake:served-model"] = ModelPricing(
        input_per_million=5.0, output_per_million=0.0
    )

    body = {"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]}
    headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}
    first = client.post("/v1/chat/completions", json=body, headers=headers)  # spend 0 -> 5, allowed
    second = client.post("/v1/chat/completions", json=body, headers=headers)  # precheck 5 >= 1 -> blocked
    assert first.status_code == 200
    assert second.status_code == 402

    metrics = client.app.state.metrics
    assert _labels_value(metrics.budget_applied_total, team_id="data-science") == 1


def test_budget_utilization_ratio_gauge_reflects_spend_over_cap(client, monkeypatch, admin_headers):
    """Not one of Document 05's original 9 metrics -- added specifically
    so deploy/prometheus/alerts.yml's 80%-of-budget rule is expressible
    at all. See app/observability/metrics.py's module docstring."""
    from app.core.pricing import ModelPricing
    from app.core.schema import Usage

    client.patch("/admin/budgets/data-science", json={"budget_cap_usd": 10.0}, headers=admin_headers)
    fake = FakeAdapter(usage_override=Usage(input_tokens=1_000_000, output_tokens=0))
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))
    client.app.state.pricing["fake:served-model"] = ModelPricing(
        input_per_million=8.5, output_per_million=0.0
    )

    resp = client.post(
        "/v1/chat/completions",
        json={"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
        headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
    )
    assert resp.status_code == 200  # spend 0 -> 8.5, still under the 10.0 cap

    metrics = client.app.state.metrics
    ratio = metrics.budget_utilization_ratio.labels(team_id="data-science")._value.get()
    assert ratio == pytest.approx(0.85)


async def test_fallback_events_total_increments_once_per_link_transition(app, monkeypatch):
    from tests.unit.conftest import running_app_client

    failing = FakeAdapter(always_fail=True, retryable=True, error_type="timeout")
    working = FakeAdapter(response_text="ok from fallback")

    def _resolve(model_id: str):
        if model_id == "openai:gpt-5.6-sol":
            return failing, "gpt-5.6-sol-served"
        if model_id == "anthropic:claude-sonnet-5":
            return working, "claude-sonnet-5-served"
        raise AssertionError(model_id)

    monkeypatch.setattr("app.api.v1_chat.resolve_model", _resolve)

    async with running_app_client(app) as client:
        await app.state.team_store.update_team("data-science", {"allowed_models": ["tier-1-reasoning"]})
        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "tier-1-reasoning", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        )
    assert resp.status_code == 200

    metrics = app.state.metrics
    assert (
        _labels_value(metrics.fallback_events_total, primary_provider="fake", fallback_provider="fake") == 1
    )


def test_circuit_breaker_state_gauge_uses_document_05_numbering(client):
    """0=Closed, 1=Half-Open, 2=Open -- NOT circuit_check.lua's own
    0/1/2=closed/open/half_open. See app/observability/metrics.py's
    module docstring; this test pins the conversion function directly so
    a future edit that reuses the Lua code by mistake fails loudly."""
    assert circuit_state_to_gauge_value("closed") == 0
    assert circuit_state_to_gauge_value("half_open") == 1
    assert circuit_state_to_gauge_value("open") == 2


async def test_circuit_breaker_state_gauge_reflects_a_tripped_circuit(client):
    cb = client.app.state.circuit_breaker
    for _ in range(5):  # CIRCUIT_BREAKER_FAILURE_THRESHOLD=5 (conftest.py)
        await cb.record_failure(provider="anthropic", model="claude-sonnet-5")

    metrics = client.app.state.metrics
    value = metrics.circuit_breaker_state.labels(provider="anthropic", model="claude-sonnet-5")._value.get()
    assert value == 2  # Open, per Document 05's numbering
