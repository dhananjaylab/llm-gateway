"""
test_hierarchical_quotas.py

Phase 8, Option A (docs/PHASE8_KICKOFF_SCOPING.md §6, developer
sign-off): org-level RPM/TPM/budget quotas above team level. Verifies:
`OrgConfigStore`'s seed/hot-reload mechanics (mirroring
`TeamConfigStore`'s, Phase 2); `RateLimiter.check_org`/`peek_org`/
`reconcile_org` and `BudgetEnforcer.precheck_org`/`record_spend_org`
(both reusing team-level Lua scripts unchanged, just re-keyed); the
end-to-end HTTP behavior — an org cap denies even when the team's own
bucket has headroom, and vice versa; and the `/admin/orgs/{org_id}`
surface (GET/PATCH/404/audit).
"""

from __future__ import annotations

import pytest

from app.core.config import DEFAULT_ORG_ID, OrgConfig, TeamConfig, load_orgs_config
from app.core.org_store import OrgConfigStore
from app.ratelimit.budget import BudgetEnforcer
from app.ratelimit.limiter import RateLimiter
from tests.unit.conftest import DATA_SCIENCE_KEY, FakeAdapter, running_app_client


def _org(**overrides) -> OrgConfig:
    defaults = {"org_id": "acme-corp", "rpm_cap": 100, "tpm_cap": 100_000, "budget_cap_usd": 100.0}
    defaults.update(overrides)
    return OrgConfig(**defaults)


# -- config loading -------------------------------------------------------


def test_load_orgs_config_synthesizes_default_org_when_file_is_missing(tmp_path):
    config = load_orgs_config(tmp_path / "does-not-exist.yaml")
    assert DEFAULT_ORG_ID in config.orgs
    assert config.orgs[DEFAULT_ORG_ID].org_id == DEFAULT_ORG_ID


def test_load_orgs_config_parses_a_real_file(tmp_path):
    p = tmp_path / "orgs.yaml"
    p.write_text('orgs:\n  acme:\n    rpm_cap: 5\n    tpm_cap: 500\n    budget_cap_usd: 12.5\n')
    config = load_orgs_config(p)
    assert config.orgs["acme"].rpm_cap == 5
    assert config.orgs["acme"].budget_cap_usd == 12.5
    # default-org still synthesized even when the file doesn't mention it.
    assert DEFAULT_ORG_ID in config.orgs


def test_team_config_defaults_to_default_org_id_backward_compatibly():
    team = TeamConfig(team_id="t1", api_key_hash="sha256:x", allowed_models=[])
    assert team.org_id == DEFAULT_ORG_ID


# -- OrgConfigStore ---------------------------------------------------------


async def test_seed_from_yaml_if_empty_writes_every_configured_org(fake_redis):
    store = OrgConfigStore(fake_redis)
    orgs_config = load_orgs_config()
    orgs_config.orgs["acme-corp"] = _org()
    seeded = await store.seed_from_yaml_if_empty(orgs_config)
    assert seeded == len(orgs_config.orgs)
    org = await store.get_org("acme-corp")
    assert org.rpm_cap == 100


async def test_seed_is_idempotent_and_skips_when_orgs_already_exist(fake_redis):
    store = OrgConfigStore(fake_redis)
    orgs_config = load_orgs_config()
    first = await store.seed_from_yaml_if_empty(orgs_config)
    second = await store.seed_from_yaml_if_empty(orgs_config)
    assert first > 0
    assert second == 0


async def test_get_org_returns_none_for_an_unknown_org(fake_redis):
    store = OrgConfigStore(fake_redis)
    assert await store.get_org("does-not-exist") is None


async def test_update_org_raises_key_error_for_an_unknown_org(fake_redis):
    store = OrgConfigStore(fake_redis)
    with pytest.raises(KeyError):
        await store.update_org("does-not-exist", {"rpm_cap": 5})


async def test_update_org_persists_and_invalidates_cache(fake_redis):
    store = OrgConfigStore(fake_redis)
    await store.seed_from_yaml_if_empty(load_orgs_config())
    updated = await store.update_org(DEFAULT_ORG_ID, {"rpm_cap": 42})
    assert updated.rpm_cap == 42
    reread = await store.get_org(DEFAULT_ORG_ID, use_cache=False)
    assert reread.rpm_cap == 42


async def test_update_org_publishes_a_config_change_event(fake_redis):
    store = OrgConfigStore(fake_redis)
    await store.seed_from_yaml_if_empty(load_orgs_config())
    pubsub = fake_redis.pubsub()
    await pubsub.subscribe(OrgConfigStore.CONFIG_CHANGE_CHANNEL)
    await pubsub.get_message(timeout=1)  # drain subscribe confirmation

    await store.update_org(DEFAULT_ORG_ID, {"rpm_cap": 7})

    msg = await pubsub.get_message(timeout=1)
    assert msg is not None
    assert msg["data"] == DEFAULT_ORG_ID
    await pubsub.unsubscribe(OrgConfigStore.CONFIG_CHANGE_CHANNEL)


# -- RateLimiter / BudgetEnforcer org-level methods --------------------------


async def test_check_org_enforces_rpm_independently_of_any_team(fake_redis):
    limiter = RateLimiter(fake_redis)
    org = _org(rpm_cap=1)
    first = await limiter.check_org(org, estimated_tokens=10)
    assert first.allowed is True
    second = await limiter.check_org(org, estimated_tokens=10)
    assert second.allowed is False
    assert second.limit_type == "rpm"


async def test_org_and_team_buckets_are_in_separate_redis_namespaces(fake_redis):
    """rl:org:{id}:... must never collide with rl:{team_id}:... even if
    the id strings are identical."""
    limiter = RateLimiter(fake_redis)
    org = _org(org_id="shared-name", rpm_cap=1)
    team = TeamConfig(team_id="shared-name", api_key_hash="sha256:x", allowed_models=[], rpm_cap=1)

    org_decision = await limiter.check_org(org, estimated_tokens=1)
    team_decision = await limiter.check(team=team, estimated_tokens=1, priority="normal")
    assert org_decision.allowed is True
    assert team_decision.allowed is True, "the team bucket must be untouched by the org check"


async def test_reconcile_org_refunds_the_unused_tpm_reservation(fake_redis):
    limiter = RateLimiter(fake_redis)
    org = _org(tpm_cap=1000)
    await limiter.check_org(org, estimated_tokens=900)
    await limiter.reconcile_org(org=org, reserved_tokens=900, actual_tokens=100)
    peek = await limiter.peek_org(org)
    assert peek.remaining_tpm == 900  # 1000 - 100 actually used


async def test_precheck_org_and_record_spend_org_track_an_independent_ledger(fake_redis):
    enforcer = BudgetEnforcer(fake_redis)
    org = _org(budget_cap_usd=10.0)

    before = await enforcer.precheck_org(org)
    assert before.allowed is True
    assert before.spend_usd == 0.0

    await enforcer.record_spend_org(org, 6.0)
    mid = await enforcer.precheck_org(org)
    assert mid.spend_usd == 6.0
    assert mid.allowed is True  # 6 < 10

    await enforcer.record_spend_org(org, 5.0)
    after = await enforcer.precheck_org(org)
    assert after.spend_usd == 11.0
    assert after.allowed is False  # 11 >= 10


# -- end-to-end through the real HTTP pipeline -------------------------------


async def test_org_budget_cap_blocks_even_though_the_team_has_full_headroom(app, monkeypatch, admin_headers):
    """The core Option-A guarantee: an org-wide block must deny a request
    the team-level bucket alone would have allowed."""
    from app.core.pricing import ModelPricing
    from app.core.schema import Usage

    fake = FakeAdapter(usage_override=Usage(input_tokens=1_000_000, output_tokens=0))
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    async with running_app_client(app) as client:
        app.state.pricing["fake:served-model"] = ModelPricing(input_per_million=5.0, output_per_million=0.0)
        # Team has generous headroom...
        await app.state.team_store.update_team(
            "data-science", {"rpm_cap": 1000, "tpm_cap": 10_000_000, "budget_cap_usd": 100_000.0}
        )
        # ...but the org cap is tiny. Budget is post-paid metered spend
        # (same semantics as team-level — see budget.py's own docstring):
        # the request that CROSSES the cap still bills; the NEXT one is
        # what gets blocked.
        await app.state.org_store.update_org(DEFAULT_ORG_ID, {"budget_cap_usd": 0.00001})

        body = {"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]}
        headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}

        first = await client.post("/v1/chat/completions", json=body, headers=headers)
        assert first.status_code == 200  # org spend 0 -> 5.0, allowed (0 < 0.00001)

        resp = await client.post("/v1/chat/completions", json=body, headers=headers)
        assert resp.status_code == 402
        body_detail = resp.json()["detail"]["error"]
        assert body_detail["level"] == "org"
        assert body_detail["org_id"] == DEFAULT_ORG_ID
        assert fake.call_count == 1, "the second (org-budget-blocked) request must never reach the provider"

        # Restore for any later test sharing this container's Redis.
        await app.state.org_store.update_org(DEFAULT_ORG_ID, {"budget_cap_usd": 5000.0})


async def test_org_rpm_cap_blocks_even_though_the_team_has_full_headroom(app, monkeypatch):
    fake = FakeAdapter(response_text="ok")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    async with running_app_client(app) as client:
        await app.state.team_store.update_team("data-science", {"rpm_cap": 1000, "tpm_cap": 10_000_000})
        await app.state.org_store.update_org(DEFAULT_ORG_ID, {"rpm_cap": 1, "tpm_cap": 10_000_000})

        headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}
        body = {"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]}

        first = await client.post("/v1/chat/completions", json=body, headers=headers)
        assert first.status_code == 200

        second = await client.post("/v1/chat/completions", json=body, headers=headers)
        assert second.status_code == 429
        detail = second.json()["detail"]["error"]
        assert detail["level"] == "org"
        assert detail["limit_type"] == "org_rpm"
        assert fake.call_count == 1, "the second (org-blocked) request must never reach the provider"

        await app.state.org_store.update_org(DEFAULT_ORG_ID, {"rpm_cap": 1000})


async def test_team_cap_still_blocks_when_the_org_has_full_headroom(app, monkeypatch):
    """The converse of the two tests above — org-level enforcement must
    be additive, never a replacement for the already-tested team-level
    path (test_token_bucket_concurrency.py / test_budget_enforcement.py
    already prove team-level behavior in isolation; this just confirms
    it still fires with a generous org sitting on top of it)."""
    fake = FakeAdapter(response_text="ok")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    async with running_app_client(app) as client:
        await app.state.team_store.update_team("data-science", {"rpm_cap": 1, "tpm_cap": 10_000_000})
        await app.state.org_store.update_org(DEFAULT_ORG_ID, {"rpm_cap": 1000, "tpm_cap": 10_000_000})

        headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}
        body = {"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]}

        first = await client.post("/v1/chat/completions", json=body, headers=headers)
        assert first.status_code == 200
        second = await client.post("/v1/chat/completions", json=body, headers=headers)
        assert second.status_code == 429
        detail = second.json()["detail"]["error"]
        # Team-level 429 body is unchanged from pre-Phase-8 -- no "level" key.
        assert "level" not in detail

        await app.state.org_store.update_org(DEFAULT_ORG_ID, {"rpm_cap": 1000})


async def test_a_successful_request_reconciles_and_bills_both_team_and_org(app, monkeypatch, admin_headers):
    from app.core.pricing import ModelPricing
    from app.core.schema import Usage

    fake = FakeAdapter(usage_override=Usage(input_tokens=1_000_000, output_tokens=0))
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    async with running_app_client(app) as client:
        app.state.pricing["fake:served-model"] = ModelPricing(input_per_million=2.0, output_per_million=0.0)

        resp = await client.post(
            "/v1/chat/completions",
            json={"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]},
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        )
        assert resp.status_code == 200

        org_view = await client.get(f"/admin/orgs/{DEFAULT_ORG_ID}", headers=admin_headers)
        assert org_view.status_code == 200
        # 1,000,000 input tokens * $2.0/1e6 = $2.00, billed to the org ledger too.
        assert org_view.json()["spend_usd"] == pytest.approx(2.0)


# -- Admin API ----------------------------------------------------------


def test_get_org_requires_admin_key(client):
    resp = client.get(f"/admin/orgs/{DEFAULT_ORG_ID}")
    assert resp.status_code == 422


def test_get_org_returns_seeded_defaults(client, admin_headers):
    resp = client.get(f"/admin/orgs/{DEFAULT_ORG_ID}", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["org_id"] == DEFAULT_ORG_ID
    assert data["rpm_cap"] == 1000
    assert data["remaining_rpm"] == 1000
    assert data["spend_usd"] == 0.0


def test_get_org_404s_for_an_unknown_org(client, admin_headers):
    resp = client.get("/admin/orgs/does-not-exist", headers=admin_headers)
    assert resp.status_code == 404
    assert resp.json()["detail"]["error"]["type"] == "org_not_found"


def test_patch_org_updates_and_writes_an_audit_entry(client, admin_headers):
    resp = client.patch(f"/admin/orgs/{DEFAULT_ORG_ID}", json={"rpm_cap": 55}, headers=admin_headers)
    assert resp.status_code == 200
    assert resp.json()["rpm_cap"] == 55

    audit = client.get("/admin/audit", headers=admin_headers).json()
    latest = audit[0]
    assert latest["action"] == "patch_org"
    assert latest["team_id"] == DEFAULT_ORG_ID
    assert latest["after"]["rpm_cap"] == 55


def test_patch_org_404s_for_an_unknown_org(client, admin_headers):
    resp = client.patch("/admin/orgs/does-not-exist", json={"rpm_cap": 5}, headers=admin_headers)
    assert resp.status_code == 404


def test_patch_org_takes_effect_on_the_very_next_request(client, monkeypatch, admin_headers):
    fake = FakeAdapter(response_text="ok")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    patch = client.patch(f"/admin/orgs/{DEFAULT_ORG_ID}", json={"rpm_cap": 1}, headers=admin_headers)
    assert patch.status_code == 200

    headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}
    body = {"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]}
    first = client.post("/v1/chat/completions", json=body, headers=headers)
    second = client.post("/v1/chat/completions", json=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 429, "the new 1-RPM org cap must already be enforced with no restart"

    client.patch(f"/admin/orgs/{DEFAULT_ORG_ID}", json={"rpm_cap": 1000}, headers=admin_headers)
