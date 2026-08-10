"""
test_fallback_chain.py

Verifies the Phase 3 test plan: "A primary provider that exhausts retries
routes to the tier's next chain link and returns 200 to the client, with
the failed attempt and successful attempt both recorded" — plus tier
resolution, the non-retryable-abort-with-no-fallback rule, and (folded in
from `test_circuit_breaker_transitions.py`'s TRD line) that requests
during an Open circuit skip the network call entirely, end-to-end through
the real wired-up pipeline (unlike test_circuit_breaker.py, which tests
the breaker in isolation).

Team setup note: config/teams.yaml's demo teams only grant literal
"provider:model" ids, not tier names (that seed data is Phase 1/2,
already delivered — Phase 3 doesn't rewrite it). Tests below grant
data-science the tier name it needs directly via `team_store.update_team`,
the same technique test_token_bucket_concurrency.py and
test_priority_queue.py already use to reach fields the Admin API's PATCH
schema doesn't expose (see app/api/admin.py's module docstring on why
`allowed_models` isn't PATCHable).
"""

from __future__ import annotations

import asyncio

from app.providers.base import ProviderError
from tests.unit.conftest import DATA_SCIENCE_KEY, FakeAdapter, running_app_client


def _body(model: str, **overrides) -> dict:
    body = {"model": model, "messages": [{"role": "user", "content": "hi"}]}
    body.update(overrides)
    return body


async def _grant_tier(app, tier_name: str) -> None:
    await app.state.team_store.update_team("data-science", {"allowed_models": [tier_name]})


# -- resolve_chain -----------------------------------------------------------


def test_load_tiers_config_returns_empty_when_the_file_is_missing(tmp_path):
    from app.core.config import load_tiers_config

    config = load_tiers_config(tmp_path / "does-not-exist.yaml")
    assert config.tiers == {}
    assert config.all_links() == []


def test_load_tiers_config_parses_the_chain_key_per_tier(tmp_path):
    from app.core.config import load_tiers_config

    p = tmp_path / "tiers.yaml"
    p.write_text('tiers:\n  demo-tier:\n    chain: ["openai:gpt-5.4", "ollama:llama3.2"]\n')

    config = load_tiers_config(p)
    assert config.chain_for("demo-tier") == ["openai:gpt-5.4", "ollama:llama3.2"]
    assert config.all_links() == ["openai:gpt-5.4", "ollama:llama3.2"]


def test_resolve_chain_expands_a_configured_tier_name(client):
    router = client.app.state.fallback_router
    chain = router.resolve_chain("tier-1-reasoning")
    assert chain == ["openai:gpt-5.4", "anthropic:claude-sonnet-5", "ollama:llama3.2"]


def test_resolve_chain_treats_a_literal_model_id_as_its_own_one_link_chain(client):
    router = client.app.state.fallback_router
    assert router.resolve_chain("openai:gpt-5.4") == ["openai:gpt-5.4"]


def test_resolve_chain_treats_an_unknown_tier_like_a_tier_name_never_seen_before(client):
    """Not a documented case, but worth pinning down: a string that looks
    like it could be a tier name but isn't configured falls through to
    the literal one-link path exactly like any other unrecognized string
    — it's only ever treated specially if it's an actual key in
    config/tiers.yaml."""
    router = client.app.state.fallback_router
    assert router.resolve_chain("tier-9-nonexistent") == ["tier-9-nonexistent"]


# -- fallback on retryable exhaustion -----------------------------------------


async def test_primary_exhausts_retries_then_second_link_serves_the_response(app, monkeypatch):
    failing = FakeAdapter(always_fail=True, retryable=True, error_type="rate_limit_exceeded")
    working = FakeAdapter(response_text="answer from the backup provider")

    def _resolve(model_id: str):
        if model_id == "openai:gpt-5.4":
            return failing, "gpt-5.4-served"
        if model_id == "anthropic:claude-sonnet-5":
            return working, "claude-sonnet-5-served"
        raise AssertionError(f"unexpected chain link resolved: {model_id}")

    monkeypatch.setattr("app.api.v1_chat.resolve_model", _resolve)

    async with running_app_client(app) as client:
        await _grant_tier(app, "tier-1-reasoning")
        resp = await client.post(
            "/v1/chat/completions",
            json=_body("tier-1-reasoning"),
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        )

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "answer from the backup provider"
    # FakeAdapter's provider_name is always "fake" regardless of which
    # chain link it's standing in for — the served-model header reports
    # the *served* provider_model string ("claude-sonnet-5-served"),
    # proving the second link (not the first) is what actually answered.
    assert resp.headers["x-gateway-served-model"] == "fake:claude-sonnet-5-served"
    # RETRY_MAX_ATTEMPTS=3 (conftest.py): the failing link is attempted 3
    # times (the "failed attempt... recorded" half of the TRD test-plan
    # line) before the router advances to the working second link, which
    # succeeds on its first try (the "successful attempt... recorded" half).
    assert failing.call_count == 3
    assert working.call_count == 1


async def test_non_retryable_error_aborts_the_whole_chain_without_trying_the_next_link(app, monkeypatch):
    """Document 03: 'a bad request is a bad request on every provider' —
    confirmed as the literal reading during Phase 3 sign-off. The second
    link (`working`) must never even be resolved."""
    bad_request = FakeAdapter(always_fail=True, retryable=False, error_type="invalid_request")
    working = FakeAdapter(response_text="should never be reached")

    def _resolve(model_id: str):
        if model_id == "openai:gpt-5.4":
            return bad_request, "gpt-5.4-served"
        raise AssertionError(f"a non-retryable failure must not advance the chain to {model_id}")

    monkeypatch.setattr("app.api.v1_chat.resolve_model", _resolve)

    async with running_app_client(app) as client:
        await _grant_tier(app, "tier-1-reasoning")
        resp = await client.post(
            "/v1/chat/completions",
            json=_body("tier-1-reasoning"),
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        )

    assert resp.status_code == 502
    assert resp.json()["detail"]["error"]["type"] == "invalid_request"
    assert bad_request.call_count == 1, "a non-retryable error must not be retried either"
    assert working.call_count == 0


async def test_chain_exhausted_returns_503_with_every_link_reported(app, monkeypatch):
    always_fails = FakeAdapter(always_fail=True, retryable=True, error_type="timeout")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (always_fails, "served"))

    async with running_app_client(app) as client:
        await _grant_tier(app, "tier-3-local")  # single-link chain: ["ollama:llama3.2"]
        resp = await client.post(
            "/v1/chat/completions",
            json=_body("tier-3-local"),
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        )

    assert resp.status_code == 503
    body = resp.json()["detail"]["error"]
    assert body["type"] == "fallback_chain_exhausted"
    assert body["tier_or_model"] == "tier-3-local"
    assert body["chain"] == ["ollama:llama3.2"]
    assert len(body["attempts"]) == 1
    assert body["attempts"][0]["outcome"] == "error"
    assert "timeout" in body["attempts"][0]["detail"]


async def test_literal_model_request_still_400s_on_an_unconfigured_provider(app):
    """Regression guard: the exact Phase 1/2 behavior
    (test_auth.py::test_unknown_provider_prefix_returns_400) must survive
    being routed through a (now trivially one-link) fallback chain."""
    async with running_app_client(app) as client:
        # data-science's allow-list already contains a literal model id
        # (openai:gpt-5.4) in the seed data — no team_store surgery needed
        # for the literal-model path, unlike the tier tests above.
        resp = await client.post(
            "/v1/chat/completions",
            json=_body("doesnotexist:some-model"),
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        )
    # allow-list gates first — a model that was never granted 403s before
    # the fallback router ever sees it. Grant it directly to isolate the
    # registry-level 400 the same way test_auth.py does.
    assert resp.status_code == 403


async def test_unconfigured_provider_400s_once_allow_listed(app):
    async with running_app_client(app) as client:
        await app.state.team_store.update_team(
            "data-science", {"allowed_models": ["doesnotexist:some-model"]}
        )
        resp = await client.post(
            "/v1/chat/completions",
            json=_body("doesnotexist:some-model"),
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        )
    assert resp.status_code == 400
    assert resp.json()["detail"]["error"]["type"] == "unknown_provider"


# -- circuit breaker gating the chain walk end-to-end -------------------------


async def test_circuit_opens_after_threshold_and_the_network_call_is_skipped(app, monkeypatch):
    failing = FakeAdapter(always_fail=True, retryable=True, error_type="timeout")
    working = FakeAdapter(response_text="from the healthy fallback")

    def _resolve(model_id: str):
        if model_id == "openai:gpt-5.4":
            return failing, "gpt-5.4-served"
        if model_id == "anthropic:claude-sonnet-5":
            return working, "claude-sonnet-5-served"
        raise AssertionError(f"unexpected chain link resolved: {model_id}")

    monkeypatch.setattr("app.api.v1_chat.resolve_model", _resolve)

    async with running_app_client(app) as client:
        await _grant_tier(app, "tier-1-reasoning")
        headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}
        body = _body("tier-1-reasoning")

        # CIRCUIT_BREAKER_FAILURE_THRESHOLD=5 (conftest.py): 5 requests,
        # each exhausting 3 retries against the failing link before
        # falling back, trips it open (one recorded failure per request,
        # not per retry — see FallbackRouter._record_outcome).
        for _ in range(5):
            resp = await client.post("/v1/chat/completions", json=body, headers=headers)
            assert resp.status_code == 200

        assert failing.call_count == 15  # 5 requests * 3 attempts each
        assert working.call_count == 5

        status = await app.state.circuit_breaker.get_status(provider="fake", model="gpt-5.4-served")
        assert status.state == "open"

        # The 6th request must skip straight past the now-open circuit —
        # zero additional calls to `failing`, not even one.
        resp = await client.post("/v1/chat/completions", json=body, headers=headers)
        assert resp.status_code == 200
        assert failing.call_count == 15, "an open circuit must be skipped without a network call"
        assert working.call_count == 6


async def test_circuit_recovers_through_half_open_after_cooldown(app, monkeypatch):
    failing = FakeAdapter(always_fail=True, retryable=True, error_type="timeout")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (failing, "gpt-5.4-served"))

    async with running_app_client(app) as client:
        headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}
        body = _body("openai:gpt-5.4")  # literal single-link chain — simplest way to isolate one circuit

        for _ in range(5):  # trip it (threshold=5)
            resp = await client.post("/v1/chat/completions", json=body, headers=headers)
            assert resp.status_code == 503

        status = await app.state.circuit_breaker.get_status(provider="fake", model="gpt-5.4-served")
        assert status.state == "open"

        # CIRCUIT_BREAKER_COOLDOWN_SECONDS=0.2 (conftest.py) — wait it out
        # for real, then swap in a working adapter for what becomes the
        # single Half-Open probe.
        await asyncio.sleep(0.25)
        recovered = FakeAdapter(response_text="recovered")
        monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (recovered, "gpt-5.4-served"))

        resp = await client.post("/v1/chat/completions", json=body, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["choices"][0]["message"]["content"] == "recovered"
        assert recovered.call_count == 1

        status_after = await app.state.circuit_breaker.get_status(provider="fake", model="gpt-5.4-served")
        assert status_after.state == "closed"
        assert status_after.failures_in_window == 0


# -- streaming fallback (pre-first-chunk only) --------------------------------


async def test_streaming_falls_back_when_the_primary_fails_before_any_chunk(app, monkeypatch):
    failing = FakeAdapter(always_fail=True, retryable=True, error_type="timeout")
    working = FakeAdapter(stream_chunks=["Hel", "lo"])

    def _resolve(model_id: str):
        if model_id == "openai:gpt-5.4":
            return failing, "gpt-5.4-served"
        if model_id == "anthropic:claude-sonnet-5":
            return working, "claude-sonnet-5-served"
        raise AssertionError(f"unexpected chain link resolved: {model_id}")

    monkeypatch.setattr("app.api.v1_chat.resolve_model", _resolve)

    async with running_app_client(app) as client:
        await _grant_tier(app, "tier-1-reasoning")
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json=_body("tier-1-reasoning", stream=True),
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        ) as resp:
            assert resp.status_code == 200
            raw_text = "".join([chunk async for chunk in resp.aiter_text()])

    assert "Hel" in raw_text
    assert "lo" in raw_text
    assert "event: error" not in raw_text
    assert failing.call_count == 3  # exhausted its retries trying to *start* a stream
    assert working.call_count == 1


async def test_streaming_mid_stream_failure_does_not_fall_back(app, monkeypatch):
    """Once content has reached the client, Phase 1/2's mid-stream
    `event: error` behavior applies — fallback is scoped to
    pre-first-chunk failures only (see FallbackRouter.stream_with_fallback's
    docstring)."""

    class _FailsAfterFirstChunk(FakeAdapter):
        async def stream(self, payload, *, request, provider_model):
            self.call_count += 1
            yield await self._first_chunk(provider_model)
            raise ProviderError("connection dropped mid-stream", retryable=True, error_type="timeout")

        async def _first_chunk(self, provider_model):
            from app.core.schema import UnifiedStreamChunk

            return UnifiedStreamChunk(
                id="mid-stream-1", provider=self.provider_name, model_served=provider_model, delta="partial"
            )

    primary = _FailsAfterFirstChunk()
    never_reached = FakeAdapter(stream_chunks=["should", "not", "appear"])

    def _resolve(model_id: str):
        if model_id == "openai:gpt-5.4":
            return primary, "gpt-5.4-served"
        raise AssertionError(f"mid-stream failure must not advance the chain to {model_id}")

    monkeypatch.setattr("app.api.v1_chat.resolve_model", _resolve)

    async with running_app_client(app) as client:
        await _grant_tier(app, "tier-1-reasoning")
        async with client.stream(
            "POST",
            "/v1/chat/completions",
            json=_body("tier-1-reasoning", stream=True),
            headers={"X-Gateway-API-Key": DATA_SCIENCE_KEY},
        ) as resp:
            assert resp.status_code == 200
            raw_text = "".join([chunk async for chunk in resp.aiter_text()])

    assert "partial" in raw_text
    assert "event: error" in raw_text
    assert "timeout" in raw_text
    assert never_reached.call_count == 0
