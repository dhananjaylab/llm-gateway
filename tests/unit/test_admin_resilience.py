"""
test_admin_resilience.py

Covers the Phase 3 addition (developer sign-off, ahead of Phase 4's
Grafana Operations dashboard): GET /admin/health and GET /admin/circuits,
read-only, gated by the same X-Gateway-Admin-Key as every other /admin/*
route.
"""

from __future__ import annotations

import asyncio


def test_get_health_requires_admin_key(client):
    resp = client.get("/admin/health")
    assert resp.status_code == 422


def test_get_health_rejects_a_wrong_admin_key(client):
    resp = client.get("/admin/health", headers={"X-Gateway-Admin-Key": "wrong"})
    assert resp.status_code == 401


def test_get_health_lists_every_provider_model_pair_reachable_from_tiers_yaml(client, admin_headers):
    resp = client.get("/admin/health", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()

    served = {(d["provider"], d["model"]) for d in data}
    # config/tiers.yaml's three chains, deduplicated: openai:gpt-5.4,
    # anthropic:claude-sonnet-5, anthropic:claude-haiku-4-5, ollama:llama3.2.
    assert ("openai", "gpt-5.4") in served
    assert ("anthropic", "claude-sonnet-5") in served
    assert ("anthropic", "claude-haiku-4-5") in served
    assert ("ollama", "llama3.2") in served

    # Nothing has been called yet in this fresh test app — every pair
    # starts "unknown", not a false "healthy".
    for entry in data:
        assert entry["state"] == "unknown"
        assert entry["sample_count"] == 0


def test_get_health_reflects_a_recorded_outcome(client, admin_headers):
    async def _record():
        await client.app.state.health_tracker.record_outcome(
            provider="openai", model="gpt-5.4", ok=False, latency_ms=250.0
        )

    asyncio.run(_record())

    resp = client.get("/admin/health", headers=admin_headers)
    entry = next(d for d in resp.json() if d["provider"] == "openai" and d["model"] == "gpt-5.4")
    assert entry["sample_count"] == 1
    assert entry["error_rate"] == 1.0
    assert entry["state"] in {"degraded", "down"}  # a single sample can't be "healthy" after a failure


def test_get_circuits_requires_admin_key(client):
    resp = client.get("/admin/circuits")
    assert resp.status_code == 422


def test_get_circuits_reports_closed_for_every_pair_by_default(client, admin_headers):
    resp = client.get("/admin/circuits", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 4
    for entry in data:
        assert entry["state"] == "closed"
        assert entry["failures_in_window"] == 0
        assert entry["opened_at"] is None


def test_get_circuits_reflects_a_tripped_circuit(client, admin_headers):
    async def _trip():
        cb = client.app.state.circuit_breaker
        for _ in range(5):  # CIRCUIT_BREAKER_FAILURE_THRESHOLD=5 (conftest.py)
            await cb.record_failure(provider="anthropic", model="claude-sonnet-5")

    asyncio.run(_trip())

    resp = client.get("/admin/circuits", headers=admin_headers)
    entry = next(
        d for d in resp.json() if d["provider"] == "anthropic" and d["model"] == "claude-sonnet-5"
    )
    assert entry["state"] == "open"
    assert entry["opened_at"] is not None
    assert entry["failures_in_window"] == 5

    # Every other pair is unaffected — circuits are independent per provider-model.
    others = [
        d for d in resp.json() if not (d["provider"] == "anthropic" and d["model"] == "claude-sonnet-5")
    ]
    assert all(d["state"] == "closed" for d in others)
