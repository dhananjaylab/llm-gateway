"""
test_dashboard_accuracy.py

Document 06 Phase 5 test plan: "Dashboard accuracy... do displayed
metrics match reality?" Document 06 itself calls this a manual checklist
item ("Manual dashboard QA... a checklist, not an automated test, but
belongs in the phase sign-off") because confirming every Grafana panel
resolves with no "no data" genuinely needs a human looking at a live
Grafana instance.

This file automates the half of that check that doesn't need a human:
query Prometheus's own HTTP API directly after a scripted request
sequence and assert the counter it reports matches the exact count of
requests just sent. If this passes but a panel still shows "no data" in
Grafana, the bug is in a panel's PromQL or Grafana's datasource wiring,
not in whether the gateway is exporting correct numbers -- narrowing
exactly where to look is the point of separating this from the manual
dashboard walkthrough.
"""

from __future__ import annotations

import asyncio
import os

import httpx

from tests.integration.conftest import GATEWAY_BASE_URL, PROMETHEUS_BASE_URL, requires_live_stack

TEAM_HEADERS = {"X-Gateway-API-Key": "sk-gw-datascience-demo-001"}
ADMIN_HEADERS = {"X-Gateway-Admin-Key": os.environ.get("GATEWAY_ADMIN_KEY", "dev-admin-key-change-me")}

pytestmark = requires_live_stack

# Prometheus's own scrape_interval (deploy/prometheus/prometheus.yml) is
# 10s -- give it a few scrape cycles' worth of polling headroom rather
# than assuming the exact interval has already elapsed by the time this
# test's HTTP query runs.
_POLL_ATTEMPTS = 20
_POLL_DELAY_SECONDS = 1.0


async def _sum_requests_total(prom_client: httpx.AsyncClient, *, team_id: str, status: str) -> float:
    query = f'sum(gen_ai_requests_total{{team_id="{team_id}",status="{status}"}})'
    resp = await prom_client.get("/api/v1/query", params={"query": query})
    resp.raise_for_status()
    result = resp.json().get("data", {}).get("result", [])
    return float(result[0]["value"][1]) if result else 0.0


async def test_prometheus_reflects_the_exact_count_of_a_scripted_request_sequence():
    async with (
        httpx.AsyncClient(base_url=GATEWAY_BASE_URL, timeout=30.0) as gateway_client,
        httpx.AsyncClient(base_url=PROMETHEUS_BASE_URL, timeout=10.0) as prom_client,
    ):
        await gateway_client.patch(
            "/admin/limits/data-science",
            json={"rpm_cap": 1000, "tpm_cap": 10_000_000},
            headers=ADMIN_HEADERS,
        )
        await gateway_client.patch(
            "/admin/budgets/data-science", json={"budget_cap_usd": 1000.0}, headers=ADMIN_HEADERS
        )

        before = await _sum_requests_total(prom_client, team_id="data-science", status="200")

        request_count = 7
        body = {"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]}
        for _ in range(request_count):
            resp = await gateway_client.post("/v1/chat/completions", json=body, headers=TEAM_HEADERS)
            assert resp.status_code == 200

        after = before
        for _ in range(_POLL_ATTEMPTS):
            after = await _sum_requests_total(prom_client, team_id="data-science", status="200")
            if after >= before + request_count:
                break
            await asyncio.sleep(_POLL_DELAY_SECONDS)

        assert after - before == request_count, (
            f"gateway sent {request_count} successful requests but Prometheus reports a delta of "
            f"{after - before} -- either the scrape hasn't caught up (increase _POLL_ATTEMPTS) or "
            f"gen_ai_requests_total is drifting from ground truth"
        )


async def test_prometheus_fallback_counter_reflects_a_scripted_failover(mock_providers_client):
    async with (
        httpx.AsyncClient(base_url=GATEWAY_BASE_URL, timeout=30.0) as gateway_client,
        httpx.AsyncClient(base_url=PROMETHEUS_BASE_URL, timeout=10.0) as prom_client,
    ):
        await gateway_client.patch(
            "/admin/limits/data-science",
            json={"rpm_cap": 1000, "tpm_cap": 10_000_000},
            headers=ADMIN_HEADERS,
        )
        await gateway_client.patch(
            "/admin/budgets/data-science", json={"budget_cap_usd": 1000.0}, headers=ADMIN_HEADERS
        )

        query = 'sum(gen_ai_fallback_events_total{primary_provider="openai",fallback_provider="anthropic"})'

        async def _current() -> float:
            resp = await prom_client.get("/api/v1/query", params={"query": query})
            resp.raise_for_status()
            result = resp.json().get("data", {}).get("result", [])
            return float(result[0]["value"][1]) if result else 0.0

        before = await _current()

        mock_providers_client.post("/_chaos/config", json={"provider": "openai", "error_rate": 1.0})
        try:
            resp = await gateway_client.post(
                "/v1/chat/completions",
                json={"model": "tier-1-reasoning", "messages": [{"role": "user", "content": "hi"}]},
                headers=TEAM_HEADERS,
            )
            assert resp.status_code == 200
        finally:
            mock_providers_client.post("/_chaos/reset")

        after = before
        for _ in range(_POLL_ATTEMPTS):
            after = await _current()
            if after >= before + 1:
                break
            await asyncio.sleep(_POLL_DELAY_SECONDS)

        assert after == before + 1
