"""
test_token_bucket_concurrency.py

Verifies the Phase 2 test plan's core atomicity claim: "100 concurrent
workers against a 10-RPM key → exactly 10 succeed, 90 receive 429 with an
accurate Retry-After (this is the core atomicity claim — must run under
real concurrency, not sequential calls)."

Uses `running_app_client` (conftest.py) rather than the sync `client`
fixture specifically because this needs TRUE concurrency: N coroutines
scheduled via `asyncio.gather()` on a single event loop, all hitting the
same fakeredis-backed Lua script. Starlette's TestClient runs the ASGI
app on a background-thread portal; firing 100 sequential `.post()` calls
against it (even from multiple Python threads) doesn't give the same
tight interleaving guarantee `asyncio.gather` does, and this test's whole
point is to catch a race, not merely exercise the happy path.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.unit.conftest import DATA_SCIENCE_KEY, FakeAdapter, running_app_client


async def _fire_concurrent_requests(app, monkeypatch, *, count: int) -> list:
    fake = FakeAdapter(response_text="ok")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    async with running_app_client(app) as client:
        # Tighten data-science's RPM cap to something small enough to
        # exhaust with 100 concurrent requests, while keeping TPM huge so
        # TPM is never the axis that denies a request in this test.
        await app.state.team_store.update_team(
            "data-science", {"rpm_cap": 10, "tpm_cap": 10_000_000}
        )

        body = {"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]}
        headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}

        responses = await asyncio.gather(
            *[client.post("/v1/chat/completions", json=body, headers=headers) for _ in range(count)]
        )
        return responses


@pytest.mark.parametrize("run", range(3))  # repeat within one test session for extra confidence
async def test_exactly_rpm_cap_requests_succeed_under_concurrency(app, monkeypatch, run):
    responses = await _fire_concurrent_requests(app, monkeypatch, count=100)

    statuses = [r.status_code for r in responses]
    succeeded = [s for s in statuses if s == 200]
    rate_limited = [s for s in statuses if s == 429]

    assert len(succeeded) == 10, f"expected exactly 10 successes, got {len(succeeded)}: {statuses}"
    assert len(rate_limited) == 90
    assert len(succeeded) + len(rate_limited) == 100, "no request should get any other status"


async def test_429_responses_carry_an_accurate_retry_after_header(app, monkeypatch):
    responses = await _fire_concurrent_requests(app, monkeypatch, count=20)

    denied = [r for r in responses if r.status_code == 429]
    assert denied, "expected at least one 429 with only 10 RPM capacity against 20 requests"
    for r in denied:
        assert "retry-after" in {k.lower() for k in r.headers}
        retry_after = int(r.headers["retry-after"])
        assert retry_after >= 1
        body = r.json()
        assert body["detail"]["error"]["type"] == "rate_limit_exceeded"
        assert body["detail"]["error"]["limit_type"] == "rpm"
        assert body["detail"]["error"]["remaining_rpm"] == 0


async def test_no_request_is_double_counted(app, monkeypatch):
    """
    A regression this test would catch: a non-atomic
    refill-then-check-then-consume sequence (e.g. three separate Redis
    round trips instead of one Lua script) lets two concurrent requests
    both observe "capacity available" and both succeed, admitting more
    than rpm_cap requests. Asserting the exact count (not <=) is the
    point — a subtly-broken script that admits 11 or 12 must fail this.
    """
    fake = FakeAdapter(response_text="ok")
    monkeypatch.setattr("app.api.v1_chat.resolve_model", lambda model_id: (fake, "served-model"))

    async with running_app_client(app) as client:
        await app.state.team_store.update_team(
            "data-science", {"rpm_cap": 5, "tpm_cap": 10_000_000}
        )
        body = {"model": "openai:gpt-5.4", "messages": [{"role": "user", "content": "hi"}]}
        headers = {"X-Gateway-API-Key": DATA_SCIENCE_KEY}

        responses = await asyncio.gather(
            *[client.post("/v1/chat/completions", json=body, headers=headers) for _ in range(50)]
        )
        succeeded = sum(1 for r in responses if r.status_code == 200)
        assert succeeded == 5
        assert fake.call_count == 5, "the adapter itself must only ever be called exactly 5 times"
