"""
test_admin_api.py

Verifies the Phase 2 test plan: "PATCH to /admin/limits/{team} updates
enforcement immediately (next request uses new caps) and writes an audit
entry; unauthorized (non-admin) callers get 401/403" — plus the matching
budget-side coverage and the empty/cold-start and unknown-team edge cases
from Document 03.
"""

from __future__ import annotations

import pytest

from tests.unit.conftest import DATA_SCIENCE_KEY, FakeAdapter

# -- auth gating --------------------------------------------------------


def test_missing_admin_key_is_rejected(client):
    resp = client.get("/admin/limits/data-science")
    assert resp.status_code == 422  # FastAPI: required header missing


def test_wrong_admin_key_is_rejected(client):
    resp = client.get("/admin/limits/data-science", headers={"X-Gateway-Admin-Key": "wrong"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["error"]["type"] == "invalid_admin_key"


def test_patch_without_admin_key_is_rejected(client):
    resp = client.patch("/admin/limits/data-science", json={"rpm_cap": 5})
    assert resp.status_code == 422


# -- limits: read, write, 404, hot-reload --------------------------------


def test_get_limits_returns_seeded_values(client, admin_headers):
    resp = client.get("/admin/limits/data-science", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["team_id"] == "data-science"
    assert data["rpm_cap"] == 100  # from config/teams.yaml
    assert data["tpm_cap"] == 50000
    assert data["remaining_rpm"] == 100  # untouched bucket, full capacity
    assert data["remaining_tpm"] == 50000


def test_get_limits_404s_for_unknown_team(client, admin_headers):
    resp = client.get("/admin/limits/does-not-exist", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["type"] == "team_not_found"


def test_patch_limits_404s_for_unknown_team(client, admin_headers):
    resp = client.patch(
        "/admin/limits/does-not-exist", json={"rpm_cap": 5}, headers=admin_headers
    )
    assert resp.status_code == 404


def test_patch_limits_takes_effect_on_the_very_next_request(client, monkeypatch, admin_headers):
    """The core hot-reload claim: no restart, visible immediately."""
    fake = FakeAdapter(response_text="ok")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    # Tighten to 1 RPM.
    patch = client.patch("/admin/limits/data-science", json={"rpm_cap": 1}, headers=admin_headers)
    assert patch.status_code == 200
    assert patch.json()["rpm_cap"] == 1

    body = {"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]}
    headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}

    first = client.post("/v1/chat/completions", json=body, headers=headers)
    second = client.post("/v1/chat/completions", json=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 429, "the new 1-RPM cap must already be enforced with no restart"


def test_patch_limits_writes_an_audit_entry(client, admin_headers):
    client.patch("/admin/limits/data-science", json={"rpm_cap": 42}, headers=admin_headers)

    audit = client.get("/admin/audit", headers=admin_headers)
    assert audit.status_code == 200
    entries = audit.json()
    assert len(entries) >= 1
    latest = entries[0]  # XREVRANGE -> newest first
    assert latest["action"] == "patch_limits"
    assert latest["team_id"] == "data-science"
    assert latest["actor"] == "admin"
    assert latest["after"]["rpm_cap"] == 42
    assert latest["before"]["rpm_cap"] == 100


def test_patch_limits_with_no_body_fields_is_a_no_op_and_writes_no_audit_entry(client, admin_headers):
    before_audit = client.get("/admin/audit", headers=admin_headers).json()

    resp = client.patch("/admin/limits/data-science", json={}, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["rpm_cap"] == 100  # unchanged

    after_audit = client.get("/admin/audit", headers=admin_headers).json()
    assert len(after_audit) == len(before_audit)


# -- budgets: read, write, 404, audit -------------------------------------


def test_get_budget_returns_seeded_values(client, admin_headers):
    resp = client.get("/admin/budgets/product-eng", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["budget_cap_usd"] == 200.00
    assert data["spend_usd"] == 0.0
    assert data["budget_period"] == "monthly"


def test_get_budget_404s_for_unknown_team(client, admin_headers):
    resp = client.get("/admin/budgets/does-not-exist", headers=admin_headers)
    assert resp.status_code == 404


def test_patch_budget_writes_an_audit_entry(client, admin_headers):
    resp = client.patch(
        "/admin/budgets/product-eng", json={"budget_cap_usd": 999.0}, headers=admin_headers
    )
    assert resp.status_code == 200
    assert resp.json()["budget_cap_usd"] == 999.0

    audit = client.get("/admin/audit", headers=admin_headers).json()
    latest = audit[0]
    assert latest["action"] == "patch_budget"
    assert latest["team_id"] == "product-eng"
    assert latest["after"]["budget_cap_usd"] == 999.0
    assert latest["before"]["budget_cap_usd"] == 200.0


# -- audit surface itself --------------------------------------------------


def test_audit_log_is_empty_on_a_fresh_deployment(client, admin_headers):
    resp = client.get("/admin/audit", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json() == []


def test_config_change_is_actually_published_on_redis_pubsub(client, admin_headers):
    """
    Confirms the cross-instance hot-reload mechanism itself (not just its
    local-cache side effect, already proven above): a PATCH really does
    PUBLISH on the documented channel, which is what a second gateway
    instance's background listener (app/main.py::_listen_for_config_changes)
    subscribes to.
    """
    from app.core.team_store import TeamConfigStore

    redis = client.app.state.redis
    pubsub = redis.pubsub()

    async def _check():
        await pubsub.subscribe(TeamConfigStore.CONFIG_CHANGE_CHANNEL)
        # Drain the subscribe-confirmation message.
        await pubsub.get_message(timeout=1)

        client.patch("/admin/limits/data-science", json={"rpm_cap": 7}, headers=admin_headers)

        msg = await pubsub.get_message(timeout=1)
        assert msg is not None
        assert msg["type"] == "message"
        assert msg["data"] == "data-science"
        await pubsub.unsubscribe(TeamConfigStore.CONFIG_CHANGE_CHANNEL)

    import asyncio

    asyncio.run(_check())


# -- Phase 9a: tiers ------------------------------------------------------


def test_get_tier_returns_the_seeded_chain(client, admin_headers):
    resp = client.get("/admin/tiers/tier-1-reasoning", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier_name"] == "tier-1-reasoning"
    assert data["chain"] == [
        "openai:gpt-5.6-sol",
        "anthropic:claude-sonnet-5",
        "ollama:llama3.2",
        "gemini:gemini-3.6-flash",
    ]


def test_get_tier_404s_for_an_unknown_tier(client, admin_headers):
    resp = client.get("/admin/tiers/tier-9-nonexistent", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["type"] == "tier_not_found"


def test_list_tiers_returns_every_seeded_tier_name(client, admin_headers):
    resp = client.get("/admin/tiers", headers=admin_headers)
    assert resp.status_code == 200
    assert set(resp.json()) >= {"tier-1-reasoning", "tier-2-fast", "tier-3-local"}


def test_patch_tier_without_admin_key_is_rejected(client):
    resp = client.patch("/admin/tiers/tier-1-reasoning", json={"chain": ["ollama:llama3.2"]})
    assert resp.status_code == 422


def test_patch_tier_404s_for_an_unknown_tier(client, admin_headers):
    resp = client.patch(
        "/admin/tiers/tier-9-nonexistent", json={"chain": ["ollama:llama3.2"]}, headers=admin_headers
    )
    assert resp.status_code == 404


def test_patch_tier_rejects_an_empty_chain(client, admin_headers):
    resp = client.patch("/admin/tiers/tier-1-reasoning", json={"chain": []}, headers=admin_headers)
    assert resp.status_code == 422


def test_patch_tier_takes_effect_on_the_very_next_request_with_no_restart(client, monkeypatch, admin_headers):
    """The core hot-reload claim for Track A, mirroring
    test_patch_limits_takes_effect_on_the_very_next_request exactly:
    FallbackRouter.resolve_chain() must see the new chain immediately."""
    patch = client.patch(
        "/admin/tiers/tier-3-local", json={"chain": ["ollama:llama3.2", "gemini:gemini-3.6-flash"]},
        headers=admin_headers,
    )
    assert patch.status_code == 200
    assert patch.json()["chain"] == ["ollama:llama3.2", "gemini:gemini-3.6-flash"]

    router = client.app.state.fallback_router
    assert router.resolve_chain("tier-3-local") == ["ollama:llama3.2", "gemini:gemini-3.6-flash"]


def test_patch_tier_writes_an_audit_entry(client, admin_headers):
    client.patch("/admin/tiers/tier-2-fast", json={"chain": ["ollama:llama3.2"]}, headers=admin_headers)

    audit = client.get("/admin/audit", headers=admin_headers).json()
    latest = audit[0]
    assert latest["action"] == "patch_tier"
    assert latest["team_id"] == "tier-2-fast"
    assert latest["after"]["chain"] == ["ollama:llama3.2"]


# -- Phase 9a: pricing ------------------------------------------------------


def test_get_pricing_returns_the_seeded_row(client, admin_headers):
    resp = client.get("/admin/pricing/openai:gpt-5.4", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["model_key"] == "openai:gpt-5.4"
    assert data["input_per_million"] == 2.5
    assert data["output_per_million"] == 15.0
    assert data["cache_read_per_million"] == 0.25


def test_get_pricing_404s_for_an_unknown_model_key(client, admin_headers):
    resp = client.get("/admin/pricing/does-not-exist", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["type"] == "pricing_not_found"


def test_list_pricing_includes_the_ollama_fallback_row(client, admin_headers):
    resp = client.get("/admin/pricing", headers=admin_headers)
    assert resp.status_code == 200
    assert "ollama:" in resp.json()


def test_patch_pricing_only_touches_the_provided_field(client, admin_headers):
    resp = client.patch(
        "/admin/pricing/openai:gpt-5.4", json={"output_per_million": 18.0}, headers=admin_headers
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["input_per_million"] == 2.5  # untouched
    assert data["output_per_million"] == 18.0  # patched


def test_patch_pricing_404s_for_an_unknown_model_key(client, admin_headers):
    resp = client.patch(
        "/admin/pricing/does-not-exist", json={"input_per_million": 1.0}, headers=admin_headers
    )
    assert resp.status_code == 404


def test_patch_pricing_rejects_a_negative_rate(client, admin_headers):
    resp = client.patch(
        "/admin/pricing/openai:gpt-5.4", json={"input_per_million": -1.0}, headers=admin_headers
    )
    assert resp.status_code == 422


def test_patch_pricing_takes_effect_on_the_very_next_cost_calculation(client, admin_headers):
    """The core hot-reload claim for Track A's pricing half: the SAME
    dict object app.state.pricing (and therefore FallbackRouter's own
    captured reference to it) must reflect the change immediately, with no
    restart -- see app/core/pricing_store.py's own in-place-mutation
    docstring for why this specifically has to be an in-place mutation."""
    from app.core.pricing import calculate_cost_usd
    from app.core.schema import Usage

    client.patch("/admin/pricing/openai:gpt-5.4", json={"input_per_million": 9.0}, headers=admin_headers)

    usage = Usage(input_tokens=1_000_000, output_tokens=0)
    cost = calculate_cost_usd(client.app.state.pricing, "openai", "gpt-5.4", usage)
    assert cost == pytest.approx(9.0)


def test_patch_pricing_writes_an_audit_entry(client, admin_headers):
    client.patch("/admin/pricing/openai:gpt-5.4", json={"input_per_million": 4.0}, headers=admin_headers)

    audit = client.get("/admin/audit", headers=admin_headers).json()
    latest = audit[0]
    assert latest["action"] == "patch_pricing"
    assert latest["team_id"] == "openai:gpt-5.4"
    assert latest["after"]["input_per_million"] == 4.0
    assert latest["before"]["input_per_million"] == 2.5


def test_patch_pricing_with_no_body_fields_is_a_no_op_and_writes_no_audit_entry(client, admin_headers):
    before_audit = client.get("/admin/audit", headers=admin_headers).json()

    resp = client.patch("/admin/pricing/openai:gpt-5.4", json={}, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["input_per_million"] == 2.5  # unchanged

    after_audit = client.get("/admin/audit", headers=admin_headers).json()
    assert len(after_audit) == len(before_audit)
