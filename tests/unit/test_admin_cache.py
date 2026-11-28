"""
test_admin_cache.py

Verifies docs/PHASE9B_KICKOFF_SCOPING.md Sec 7's admin surface --
PATCH /admin/limits/{team} accepting and persisting `semantic_cache_enabled`,
GET /admin/cache/stats/{team}, POST /admin/cache/purge/{team} -- PLUS the
regression guard for a real bug found while building this phase: the
kickoff doc's claim that a new top-level TeamConfig field "round-trips
through update_team() for free" was wrong. update_team() does a correct
in-memory model_copy, but TeamConfigStore._write_team() persists an
EXPLICIT field list, so a new field silently reverted to its default on
the next read from Redis until _write_team/_deserialize were taught about
it (same shape Phase 8's org_id needed). test_team_store_persists_... below
is that bug's own regression test.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import fakeredis
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.api.admin import router as admin_router
from app.caching.cache_gateway import CacheGateway
from app.caching.exact_cache import ExactMatchCache
from app.caching.fingerprint import scope
from app.caching.semantic_cache import SemanticCache
from app.core import config as config_module
from app.core.audit import AuditLog
from app.core.config import GatewaySettings, TeamConfig
from app.core.schema import ChatMessage, UnifiedChatRequest, UnifiedChatResponse, UnifiedChoice, Usage
from app.core.team_store import TeamConfigStore

ADMIN_KEY = "test-admin-key"


class _FixedVectorEmbedder:
    dimension = 4

    async def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0, 0.0]


@dataclass
class _Peek:
    remaining_rpm: int = 10
    remaining_tpm: int = 1000


class _StubRateLimiter:
    async def peek(self, team):
        return _Peek()


@pytest.fixture(autouse=True)
def _admin_env(monkeypatch):
    monkeypatch.setenv("GATEWAY_ADMIN_KEY", ADMIN_KEY)
    config_module.reset_gateway_settings_cache()
    yield
    config_module.reset_gateway_settings_cache()


@pytest.fixture
def admin_headers():
    return {"X-Gateway-Admin-Key": ADMIN_KEY}


async def _build_app(*, with_cache_gateway: bool = True):
    redis = fakeredis.FakeAsyncRedis(decode_responses=True, version=(8, 0, 0))
    team_store = TeamConfigStore(redis)
    await team_store._write_team(
        TeamConfig(team_id="data-science", api_key_hash="sha256:x", allowed_models=["openai:gpt-5.4", "ollama:llama3.2"])
    )

    app = FastAPI()
    app.include_router(admin_router)
    app.state.redis = redis
    app.state.team_store = team_store
    app.state.rate_limiter = _StubRateLimiter()
    app.state.audit_log = AuditLog(redis)

    if with_cache_gateway:
        exact = ExactMatchCache(redis, ttl_seconds=3600)
        semantic = SemanticCache(redis, ttl_seconds=3600, similarity_threshold=0.97, max_entries_per_scope=5000)
        app.state.cache_gateway = CacheGateway(
            exact_cache=exact,
            semantic_cache=semantic,
            embedding_provider=_FixedVectorEmbedder(),
            settings=GatewaySettings(cache_enabled=True),
        )
    return app


def _client(app):
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _response(text="answer") -> UnifiedChatResponse:
    return UnifiedChatResponse(
        id="r1",
        provider="fake",
        model_requested="openai:gpt-5.4",
        model_served="gpt-5.4-served",
        choices=[UnifiedChoice(index=0, message=ChatMessage(role="assistant", content=text), finish_reason="stop")],
        usage=Usage(input_tokens=5, output_tokens=5),
    )


# -- the bug found during the build ------------------------------------------


async def test_team_store_persists_semantic_cache_enabled_across_a_real_redis_round_trip():
    """Regression guard: without _write_team/_deserialize knowing about the
    new field, update_team() would appear to work (the in-memory
    model_copy is fine) and then silently revert on the next read."""
    redis = fakeredis.FakeAsyncRedis(decode_responses=True, version=(8, 0, 0))
    store = TeamConfigStore(redis)
    await store._write_team(TeamConfig(team_id="t1", api_key_hash="sha256:x", allowed_models=[]))

    assert (await store.get_team("t1", use_cache=False)).semantic_cache_enabled is False

    await store.update_team("t1", {"semantic_cache_enabled": True})

    reread = await store.get_team("t1", use_cache=False)  # straight from Redis, bypassing the local cache
    assert reread.semantic_cache_enabled is True


async def test_a_team_written_before_phase_9b_defaults_to_cache_off():
    """Backward compat: a team hash with no semantic_cache_enabled field at
    all (every team written before this phase shipped) must read back as
    off, not raise -- same shape Phase 8's org_id fallback used."""
    redis = fakeredis.FakeAsyncRedis(decode_responses=True, version=(8, 0, 0))
    store = TeamConfigStore(redis)
    await redis.hset(
        "team_config:legacy",
        mapping={"team_id": "legacy", "api_key_hash": "sha256:x", "allowed_models": "[]", "policy": "{}"},
    )
    team = await store.get_team("legacy", use_cache=False)
    assert team.semantic_cache_enabled is False


# -- PATCH /admin/limits/{team} ----------------------------------------------


async def test_patch_limits_accepts_and_persists_semantic_cache_enabled(admin_headers):
    app = await _build_app()
    async with _client(app) as client:
        resp = await client.patch(
            "/admin/limits/data-science", json={"semantic_cache_enabled": True}, headers=admin_headers
        )
        assert resp.status_code == 200
        assert resp.json()["semantic_cache_enabled"] is True

        got = await client.get("/admin/limits/data-science", headers=admin_headers)
        assert got.json()["semantic_cache_enabled"] is True

    persisted = await app.state.team_store.get_team("data-science", use_cache=False)
    assert persisted.semantic_cache_enabled is True


async def test_patch_limits_semantic_cache_change_is_audit_logged(admin_headers):
    app = await _build_app()
    async with _client(app) as client:
        await client.patch("/admin/limits/data-science", json={"semantic_cache_enabled": True}, headers=admin_headers)
    entries = await app.state.audit_log.recent()
    assert entries[0]["action"] == "patch_limits"
    assert entries[0]["before"]["semantic_cache_enabled"] is False
    assert entries[0]["after"]["semantic_cache_enabled"] is True


# -- GET /admin/cache/stats/{team} --------------------------------------------


async def test_cache_stats_reflects_entries_written_through_the_gateway(admin_headers):
    app = await _build_app()
    team = await app.state.team_store.get_team("data-science", use_cache=False)
    team = team.model_copy(update={"semantic_cache_enabled": True})
    req = UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="hi")])
    await app.state.cache_gateway.write_through(request=req, team=team, response=_response(), provider="fake", cost_usd=0.001)

    async with _client(app) as client:
        resp = await client.get("/admin/cache/stats/data-science", headers=admin_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["cache_enabled_globally"] is False  # env-driven GatewaySettings, unset in this test env
    by_model = {s["requested_model"]: s for s in body["scopes"]}
    assert by_model["openai:gpt-5.4"]["exact_entries"] == 1
    assert by_model["openai:gpt-5.4"]["semantic_entries"] == 1
    assert by_model["ollama:llama3.2"]["exact_entries"] == 0


async def test_cache_stats_404s_for_an_unknown_team(admin_headers):
    app = await _build_app()
    async with _client(app) as client:
        resp = await client.get("/admin/cache/stats/nope", headers=admin_headers)
    assert resp.status_code == 404


# -- POST /admin/cache/purge/{team} ------------------------------------------


async def test_purge_removes_every_scope_for_the_team_and_audit_logs_it(admin_headers):
    app = await _build_app()
    team = (await app.state.team_store.get_team("data-science", use_cache=False)).model_copy(
        update={"semantic_cache_enabled": True}
    )
    req = UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="hi")])
    await app.state.cache_gateway.write_through(request=req, team=team, response=_response(), provider="fake", cost_usd=0.001)
    s = scope(team=team, requested_model="openai:gpt-5.4")
    assert await app.state.cache_gateway._exact.get(scope=s, request=req) is not None

    async with _client(app) as client:
        resp = await client.post("/admin/cache/purge/data-science", headers=admin_headers)

    assert resp.status_code == 200
    assert resp.json()["purged_scopes"] == 2  # both allowed models
    assert await app.state.cache_gateway._exact.get(scope=s, request=req) is None
    assert await app.state.redis.exists(f"cache:sem:{s}:meta") == 0
    assert (await app.state.audit_log.recent())[0]["action"] == "purge_cache"


async def test_purge_does_not_touch_another_teams_cache(admin_headers):
    app = await _build_app()
    await app.state.team_store._write_team(
        TeamConfig(team_id="other", api_key_hash="sha256:y", allowed_models=["openai:gpt-5.4"], semantic_cache_enabled=True)
    )
    other = await app.state.team_store.get_team("other", use_cache=False)
    req = UnifiedChatRequest(model="openai:gpt-5.4", messages=[ChatMessage(role="user", content="hi")])
    await app.state.cache_gateway.write_through(request=req, team=other, response=_response(), provider="fake", cost_usd=0.001)

    async with _client(app) as client:
        await client.post("/admin/cache/purge/data-science", headers=admin_headers)

    s = scope(team=other, requested_model="openai:gpt-5.4")
    assert await app.state.cache_gateway._exact.get(scope=s, request=req) is not None


async def test_purge_404s_for_an_unknown_team(admin_headers):
    app = await _build_app()
    async with _client(app) as client:
        resp = await client.post("/admin/cache/purge/nope", headers=admin_headers)
    assert resp.status_code == 404


async def test_purge_503s_when_no_cache_gateway_is_wired(admin_headers):
    app = await _build_app(with_cache_gateway=False)
    async with _client(app) as client:
        resp = await client.post("/admin/cache/purge/data-science", headers=admin_headers)
    assert resp.status_code == 503
    assert resp.json()["detail"]["error"]["type"] == "cache_disabled"


async def test_cache_admin_routes_require_the_admin_key():
    app = await _build_app()
    async with _client(app) as client:
        assert (await client.get("/admin/cache/stats/data-science")).status_code == 422
        assert (await client.post("/admin/cache/purge/data-science")).status_code == 422
        wrong = {"X-Gateway-Admin-Key": "wrong"}
        assert (await client.post("/admin/cache/purge/data-science", headers=wrong)).status_code == 401
